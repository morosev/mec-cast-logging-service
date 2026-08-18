"""Turning telemetry snapshots into session summaries.

The recorder posts a snapshot every ``interval_s`` holding *aggregates* over
that window, never individual frames. Two consequences shape everything here:

* Extremes and counts compose across windows, so session min, max, totals and
  the count-weighted mean are exact.
* Percentiles do not. The median of window p50s is not the session p50, so it
  is reported as "typical" and the worst window is reported beside it. Nothing
  in this module invents a session-wide percentile.

Cumulative fields (``rows_written``, ``drops.samples_total``, ``seq.*``) run
for the life of the recorder, so the session total is the last value, not a
sum. Only ``drops.samples_delta`` is per-window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .schemas import (
    METRIC_NAMES,
    BudgetSplit,
    MetricSummary,
    PtpSummary,
    ServiceStats,
    SessionDetail,
    SessionTimeseries,
)

DEFAULT_SLO_NS = 100_000_000
"""100 ms. A common glass-to-glass budget for remote operation; override per request."""


def _metric(context: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The metric block for ``name``, or None when that window had no samples."""
    block = (context.get("metrics") or {}).get(name)
    return block if isinstance(block, dict) else None


def _number(value: Any) -> float | None:
    """Coerce JSON numbers, tolerating strings and rejecting bools."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    number = _number(value)
    return default if number is None else int(number)


def _median(values: Sequence[float]) -> float:
    """Lower median: an observed window value, never an interpolated one."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _cumulative_total(values: Sequence[int]) -> int:
    """Total of a counter that restarts at zero when its recorder does.

    ``rows_written`` and the drop counters climb for the life of one recorder
    instance. Restarting a component mid-run — which the recorder now supports,
    since it appends to ``samples.csv`` rather than truncating — starts a second
    instance whose counters begin again at zero. A value below the running peak
    marks that boundary, so the finished segment is banked and counting
    resumes. Taking the maximum instead would keep only the largest segment and
    silently discard the rest.

    Sessions without a restart are strictly ascending, where this returns the
    final value, exactly as the maximum did.
    """
    total = 0
    peak = 0
    for value in values:
        if value < peak:  # counter went backwards: a new instance began
            total += peak
        peak = value
    return total + peak


def _sequence_spans(seq_blocks: Sequence[dict[str, Any]]) -> list[tuple[int, int]]:
    """Contiguous ``seq`` ranges, one per recorder instance, in order.

    Sequence numbers come from the producer, so they normally climb across the
    whole session even when a consumer restarts. When the *producer* restarts
    they return to zero, and treating the run as one range would measure from
    the wrong origin. Splitting on that reset keeps each instance's span
    separate so the expected frame count is their sum.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end: int | None = None

    for block in seq_blocks:
        first = _number(block.get("first"))
        last = _number(block.get("last"))
        if first is None or last is None:
            continue
        first, last = int(first), int(last)
        if start is None:
            start, end = first, last
            continue
        if last < end or first < start:  # producer restarted
            spans.append((start, end))
            start, end = first, last
        else:
            end = max(end, last)

    if start is not None and end is not None:
        spans.append((start, end))
    return spans


def summarise_metric(blocks: Sequence[dict[str, Any]]) -> MetricSummary | None:
    """Fold per-window metric blocks into one session summary."""
    usable = [
        (block, count)
        for block in blocks
        if (count := _int(block.get("count"))) > 0 and _number(block.get("mean_ns")) is not None
    ]
    if not usable:
        return None

    total = sum(count for _, count in usable)
    weighted_mean = sum(_number(b.get("mean_ns")) * n for b, n in usable) / total

    # Pooled variance: within-window spread plus the spread between window
    # means. Windows of one sample carry no internal variance but still move
    # the mean, so they contribute only the second term.
    within = sum((n - 1) * (_number(b.get("stddev_ns")) or 0.0) ** 2 for b, n in usable)
    between = sum(n * (_number(b.get("mean_ns")) - weighted_mean) ** 2 for b, n in usable)
    stddev = ((within + between) / (total - 1)) ** 0.5 if total > 1 else 0.0

    def percentiles(key: str) -> list[float]:
        return [value for b, _ in usable if (value := _number(b.get(key))) is not None]

    p50s, p90s, p99s = percentiles("p50_ns"), percentiles("p90_ns"), percentiles("p99_ns")
    mins = [value for b, _ in usable if (value := _number(b.get("min_ns"))) is not None]
    maxes = [value for b, _ in usable if (value := _number(b.get("max_ns"))) is not None]

    return MetricSummary(
        windows=len(usable),
        samples=total,
        min_ns=int(min(mins)) if mins else 0,
        max_ns=int(max(maxes)) if maxes else 0,
        mean_ns=weighted_mean,
        stddev_ns=stddev,
        p50_typical_ns=int(_median(p50s)) if p50s else 0,
        p90_typical_ns=int(_median(p90s)) if p90s else 0,
        p99_typical_ns=int(_median(p99s)) if p99s else 0,
        p99_worst_ns=int(max(p99s)) if p99s else 0,
    )


def _slope_per_minute(elapsed_s: Sequence[float], values: Sequence[float]) -> float | None:
    """Least-squares slope in units per minute, or None when it is meaningless."""
    if len(values) < 3:
        return None
    n = len(values)
    mean_x = sum(elapsed_s) / n
    mean_y = sum(values) / n
    denominator = sum((x - mean_x) ** 2 for x in elapsed_s)
    if denominator == 0:
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(elapsed_s, values, strict=True))
    return (numerator / denominator) * 60.0


def _budget(metrics: dict[str, MetricSummary]) -> BudgetSplit | None:
    """Split the e2e mean into its three stages, with the residual made explicit."""
    e2e = metrics.get("e2e")
    if e2e is None:
        return None
    sender = metrics["sender"].mean_ns if "sender" in metrics else 0.0
    network = metrics["network"].mean_ns if "network" in metrics else 0.0
    processing = metrics["processing"].mean_ns if "processing" in metrics else 0.0
    return BudgetSplit(
        sender_ns=sender,
        network_ns=network,
        processing_ns=processing,
        unaccounted_ns=e2e.mean_ns - (sender + network + processing),
        total_ns=e2e.mean_ns,
    )


def build_session_detail(
    trace_id: str,
    rows: Sequence[Any],
    slo_threshold_ns: int = DEFAULT_SLO_NS,
) -> SessionDetail:
    """Aggregate one session's snapshot rows. ``rows`` must be time-ordered."""
    contexts = [row["context"] or {} for row in rows]
    timestamps: list[datetime] = [row["timestamp"] for row in rows]

    started_at, ended_at = timestamps[0], timestamps[-1]
    duration_s = (ended_at - started_at).total_seconds()

    metrics: dict[str, MetricSummary] = {}
    for name in METRIC_NAMES:
        blocks = [block for context in contexts if (block := _metric(context, name)) is not None]
        if (summary := summarise_metric(blocks)) is not None:
            metrics[name] = summary

    # Cumulative counters are per recorder, and each service runs its own, so
    # they are folded per service rather than across the session.
    per_service: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row, context in zip(rows, contexts, strict=True):
        per_service.setdefault((row["service"], row["host"]), []).append(context)

    by_service: list[ServiceStats] = []
    for (service, host), service_contexts in sorted(per_service.items()):
        def drops(key: str, windows: list[dict[str, Any]] = service_contexts) -> int:
            return _cumulative_total(
                [_int((context.get("drops") or {}).get(key)) for context in windows]
            )

        rows_written = _cumulative_total(
            [_int(context.get("rows_written")) for context in service_contexts]
        )

        seq_blocks = [context.get("seq") or {} for context in service_contexts]
        spans = _sequence_spans(seq_blocks)
        seq_first = spans[0][0] if spans else None
        seq_last = spans[-1][1] if spans else None
        expected = sum(last - first + 1 for first, last in spans) if spans else None
        missing = max(expected - rows_written, 0) if expected is not None else None

        by_service.append(
            ServiceStats(
                service=service,
                host=host,
                windows=len(service_contexts),
                rows_written=rows_written,
                samples_dropped=drops("samples_total"),
                snapshots_dropped=drops("snapshots"),
                seq_first=seq_first,
                seq_last=seq_last,
                frames_expected=expected,
                frames_missing=missing,
            )
        )

    ptp_blocks = [context.get("ptp") or {} for context in contexts]
    reliable = sum(1 for block in ptp_blocks if block.get("reliable") is True)
    offsets = [
        abs(value) for block in ptp_blocks if (value := _number(block.get("offset_ns"))) is not None
    ]
    ptp = PtpSummary(
        windows=len(ptp_blocks),
        reliable_windows=reliable,
        reliable_pct=100.0 * reliable / len(ptp_blocks) if ptp_blocks else 0.0,
        max_abs_offset_ns=int(max(offsets)) if offsets else None,
        trustworthy=bool(ptp_blocks) and reliable == len(ptp_blocks),
    )

    elapsed = [(stamp - started_at).total_seconds() for stamp in timestamps]
    p99_pairs = [
        (seconds, value)
        for seconds, context in zip(elapsed, contexts, strict=True)
        if (block := _metric(context, "e2e")) is not None
        and (value := _number(block.get("p99_ns"))) is not None
    ]
    drift = (
        _slope_per_minute([s for s, _ in p99_pairs], [v for _, v in p99_pairs])
        if p99_pairs
        else None
    )
    compliance = (
        100.0 * sum(1 for _, value in p99_pairs if value <= slo_threshold_ns) / len(p99_pairs)
        if p99_pairs
        else None
    )

    total_rows = sum(stats.rows_written for stats in by_service)
    intervals = [value for context in contexts if (value := _number(context.get("interval_s")))]

    return SessionDetail(
        trace_id=trace_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        windows=len(rows),
        services=sorted({row["service"] for row in rows}),
        hosts=sorted({row["host"] for row in rows if row["host"]}),
        interval_s=intervals[0] if intervals else None,
        metrics=metrics,
        by_service=by_service,
        ptp=ptp,
        budget=_budget(metrics),
        effective_rate_hz=(total_rows / duration_s) if duration_s > 0 else None,
        slo_threshold_ns=slo_threshold_ns,
        slo_compliance_pct=compliance,
        p99_drift_ns_per_min=drift,
    )


def build_timeseries(trace_id: str, rows: Sequence[Any]) -> SessionTimeseries:
    """Project snapshot rows into parallel columns for charting."""
    contexts = [row["context"] or {} for row in rows]
    timestamps: list[datetime] = [row["timestamp"] for row in rows]
    started_at = timestamps[0]

    def metric_column(name: str, key: str) -> list[float | None]:
        return [
            _number(block.get(key)) if (block := _metric(context, name)) is not None else None
            for context in contexts
        ]

    return SessionTimeseries(
        trace_id=trace_id,
        t=[stamp.timestamp() for stamp in timestamps],
        elapsed_s=[(stamp - started_at).total_seconds() for stamp in timestamps],
        service=[row["service"] for row in rows],
        e2e_p50_ns=metric_column("e2e", "p50_ns"),
        e2e_p90_ns=metric_column("e2e", "p90_ns"),
        e2e_p99_ns=metric_column("e2e", "p99_ns"),
        e2e_mean_ns=metric_column("e2e", "mean_ns"),
        e2e_min_ns=metric_column("e2e", "min_ns"),
        e2e_max_ns=metric_column("e2e", "max_ns"),
        sender_mean_ns=metric_column("sender", "mean_ns"),
        network_mean_ns=metric_column("network", "mean_ns"),
        processing_mean_ns=metric_column("processing", "mean_ns"),
        ptp_offset_ns=[
            _number((context.get("ptp") or {}).get("offset_ns")) for context in contexts
        ],
        ptp_reliable=[(context.get("ptp") or {}).get("reliable") is True for context in contexts],
        samples_delta=[
            _int((context.get("drops") or {}).get("samples_delta")) for context in contexts
        ],
        rows_written=[_int(context.get("rows_written")) for context in contexts],
    )
