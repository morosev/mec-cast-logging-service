"""Aggregation of telemetry snapshots into session summaries.

Pure functions over plain dicts, so none of this needs a database.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

import pytest

from mec_cast_logging.sessions import build_session_detail, build_timeseries, summarise_metric

START = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def metric(count, mean, stddev=0.0, p50=None, p90=None, p99=None, low=None, high=None):
    """A metric block shaped like the recorder's snapshot JSON."""
    p50 = mean if p50 is None else p50
    return {
        "count": count,
        "last_ns": mean,
        "min_ns": mean if low is None else low,
        "max_ns": mean if high is None else high,
        "mean_ns": mean,
        "stddev_ns": stddev,
        "p50_ns": p50,
        "p90_ns": p50 if p90 is None else p90,
        "p99_ns": p50 if p99 is None else p99,
    }


def window(index, *, e2e=None, sender=None, network=None, processing=None, reliable=True,
           offset_ns=100, dropped_total=0, dropped_delta=0, rows=10, seq_first=0, seq_last=9,
           service="mec-cast-edge", host="edge-1"):
    return {
        "timestamp": START + timedelta(seconds=2 * index),
        "service": service,
        "host": host,
        "context": {
            "run_id": "run-1",
            "interval_s": 2.0,
            "metrics": {
                "e2e": e2e,
                "network": network,
                "processing": processing,
                "sender": sender,
            },
            "drops": {
                "samples_total": dropped_total,
                "samples_delta": dropped_delta,
                "snapshots": 0,
            },
            "ptp": {"offset_ns": offset_ns, "reliable": reliable},
            "seq": {"first": seq_first, "last": seq_last},
            "rows_written": rows,
        },
    }


class TestSummariseMetric:
    def test_returns_none_without_usable_windows(self):
        assert summarise_metric([]) is None
        assert summarise_metric([metric(0, 5)]) is None

    def test_mean_is_count_weighted_not_a_mean_of_means(self):
        # 1 sample at 100, 99 samples at 200: the naive average of the two
        # window means would be 150, which would be wrong.
        summary = summarise_metric([metric(1, 100), metric(99, 200)])
        assert summary.samples == 100
        assert summary.mean_ns == pytest.approx((100 + 99 * 200) / 100)

    def test_extremes_compose_across_windows(self):
        summary = summarise_metric([
            metric(5, 100, low=40, high=180),
            metric(5, 100, low=70, high=250),
        ])
        assert summary.min_ns == 40
        assert summary.max_ns == 250

    def test_pooled_stddev_matches_the_combined_sample(self):
        # Two windows drawn from a known population; pooling the per-window
        # summaries must reproduce the stddev of the whole sample.
        left = [10.0, 12.0, 14.0, 16.0]
        right = [100.0, 104.0, 108.0]
        blocks = [
            metric(len(left), statistics.fmean(left), statistics.stdev(left)),
            metric(len(right), statistics.fmean(right), statistics.stdev(right)),
        ]
        summary = summarise_metric(blocks)
        assert summary.stddev_ns == pytest.approx(statistics.stdev(left + right))

    def test_percentiles_are_typical_and_worst_never_averaged(self):
        summary = summarise_metric([
            metric(10, 50, p99=60),
            metric(10, 50, p99=900),
            metric(10, 50, p99=70),
        ])
        # Median window p99, not the mean of the three.
        assert summary.p99_typical_ns == 70
        assert summary.p99_worst_ns == 900

    def test_single_sample_windows_contribute_spread_but_no_variance(self):
        summary = summarise_metric([metric(1, 10), metric(1, 20)])
        assert summary.samples == 2
        assert summary.stddev_ns == pytest.approx(statistics.stdev([10.0, 20.0]))


class TestSessionDetail:
    def test_frames_missing_separates_gaps_from_recorder_drops(self):
        # seq 0..99 means 100 expected; the recorder wrote 90 and dropped 4,
        # so 10 never arrived at all. Missing counts gaps, not drops.
        rows = [window(0, e2e=metric(90, 1_000_000), rows=90, seq_first=0, seq_last=99,
                       dropped_total=4)]
        detail = build_session_detail("run-1", rows)
        service = detail.by_service[0]
        assert service.frames_expected == 100
        assert service.rows_written == 90
        assert service.frames_missing == 10
        assert service.samples_dropped == 4

    def test_cumulative_counters_take_the_last_value_not_the_sum(self):
        rows = [
            window(0, e2e=metric(10, 1e6), rows=10, dropped_total=1, seq_last=9),
            window(1, e2e=metric(10, 1e6), rows=20, dropped_total=3, seq_last=19),
        ]
        detail = build_session_detail("run-1", rows)
        service = detail.by_service[0]
        assert service.rows_written == 20
        assert service.samples_dropped == 3

    def test_restart_banks_the_earlier_counter_segment(self):
        # A consumer restart resets rows_written to zero while the producer
        # keeps numbering, so the session total is 41 + 6, not max(41, 6).
        # These are the figures from a real run: 41 frames before the restart,
        # 6 after, and 47 rows in the appended CSV.
        rows = [
            window(0, e2e=metric(10, 1e6), rows=41, dropped_total=2, seq_first=525, seq_last=1288),
            window(1, e2e=metric(10, 1e6), rows=6, dropped_total=1, seq_first=525, seq_last=2853),
        ]
        service = build_session_detail("run-1", rows).by_service[0]
        assert service.rows_written == 47
        assert service.samples_dropped == 3
        assert service.frames_expected == 2853 - 525 + 1
        assert service.frames_missing == 2329 - 47

    def test_counters_that_never_reset_are_unchanged(self):
        # The common case must keep behaving exactly as before.
        rows = [
            window(index, e2e=metric(10, 1e6), rows=10 * (index + 1), dropped_total=index)
            for index in range(5)
        ]
        service = build_session_detail("run-1", rows).by_service[0]
        assert service.rows_written == 50
        assert service.samples_dropped == 4

    def test_producer_restart_splits_the_sequence_span(self):
        # When the producer restarts, seq returns to zero. Measuring 0..99 then
        # 0..49 as one range would claim 100 expected frames; it is 150.
        rows = [
            window(0, e2e=metric(10, 1e6), rows=100, seq_first=0, seq_last=99),
            window(1, e2e=metric(10, 1e6), rows=50, seq_first=0, seq_last=49),
        ]
        service = build_session_detail("run-1", rows).by_service[0]
        assert service.frames_expected == 150
        assert service.rows_written == 150
        assert service.frames_missing == 0

    def test_per_service_accounting_is_kept_apart(self):
        rows = [
            window(0, e2e=metric(10, 1e6), service="edge", host="a", rows=10),
            window(0, sender=metric(10, 1e5), service="lidar", host="b", rows=7),
        ]
        detail = build_session_detail("run-1", rows)
        assert [s.service for s in detail.by_service] == ["edge", "lidar"]
        assert {s.rows_written for s in detail.by_service} == {10, 7}

    def test_ptp_untrustworthy_when_any_window_is_unlocked(self):
        rows = [
            window(0, e2e=metric(5, 1e6), reliable=True, offset_ns=500),
            window(1, e2e=metric(5, 1e6), reliable=False, offset_ns=-9000),
        ]
        detail = build_session_detail("run-1", rows)
        assert detail.ptp.trustworthy is False
        assert detail.ptp.reliable_pct == pytest.approx(50.0)
        assert detail.ptp.max_abs_offset_ns == 9000

    def test_budget_residual_is_reported_rather_than_hidden(self):
        rows = [window(0,
                       e2e=metric(10, 100.0),
                       sender=metric(10, 20.0),
                       network=metric(10, 30.0),
                       processing=metric(10, 25.0))]
        budget = build_session_detail("run-1", rows).budget
        assert budget.total_ns == pytest.approx(100.0)
        assert budget.unaccounted_ns == pytest.approx(25.0)

    def test_slo_compliance_counts_windows_under_the_threshold(self):
        rows = [
            window(0, e2e=metric(10, 1e6, p99=50_000_000)),
            window(1, e2e=metric(10, 1e6, p99=150_000_000)),
            window(2, e2e=metric(10, 1e6, p99=90_000_000)),
            window(3, e2e=metric(10, 1e6, p99=99_000_000)),
        ]
        detail = build_session_detail("run-1", rows, slo_threshold_ns=100_000_000)
        assert detail.slo_compliance_pct == pytest.approx(75.0)

    def test_drift_is_positive_when_p99_climbs(self):
        rows = [
            window(index, e2e=metric(10, 1e6, p99=10_000_000 + index * 1_000_000))
            for index in range(6)
        ]
        detail = build_session_detail("run-1", rows)
        # +1 ms per 2 s window is +30 ms per minute.
        assert detail.p99_drift_ns_per_min == pytest.approx(30_000_000)

    def test_drift_is_none_when_too_few_points_to_fit(self):
        rows = [window(0, e2e=metric(10, 1e6)), window(1, e2e=metric(10, 1e6))]
        assert build_session_detail("run-1", rows).p99_drift_ns_per_min is None

    def test_absent_metrics_are_omitted_rather_than_zeroed(self):
        detail = build_session_detail("run-1", [window(0, sender=metric(10, 5e5))])
        assert "sender" in detail.metrics
        assert "e2e" not in detail.metrics
        assert detail.budget is None


class TestTimeseries:
    def test_columns_are_parallel_and_nulls_survive(self):
        rows = [
            window(0, e2e=metric(10, 1_000_000)),
            window(1, e2e=None),
            window(2, e2e=metric(10, 3_000_000)),
        ]
        series = build_timeseries("run-1", rows)
        assert len(series.t) == len(series.elapsed_s) == len(series.e2e_p50_ns) == 3
        assert series.e2e_p50_ns[1] is None
        assert series.elapsed_s == [0.0, 2.0, 4.0]

    def test_reliability_is_strictly_boolean(self):
        rows = [
            window(0, e2e=metric(1, 1), reliable=True),
            window(1, e2e=metric(1, 1), reliable=False),
        ]
        assert build_timeseries("run-1", rows).ptp_reliable == [True, False]
