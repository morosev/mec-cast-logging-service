"""Request and response models for the public API."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LogLevel(StrEnum):
    """Severity levels, mirroring the Python stdlib ``logging`` names."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def severity(self) -> int:
        """Numeric severity, matching stdlib ``logging`` values."""
        return _SEVERITY[self]


_SEVERITY: dict[LogLevel, int] = {
    LogLevel.DEBUG: 10,
    LogLevel.INFO: 20,
    LogLevel.WARNING: 30,
    LogLevel.ERROR: 40,
    LogLevel.CRITICAL: 50,
}


class LogEntryCreate(BaseModel):
    """A single log record submitted by a client."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime | None = Field(
        default=None,
        description="When the event happened. Defaults to ingestion time. Naive values are UTC.",
    )
    level: LogLevel
    service: str = Field(min_length=1, max_length=128, description="Emitting mec-cast app.")
    host: str | None = Field(default=None, max_length=255)
    logger: str | None = Field(default=None, max_length=255)
    message: str = Field(min_length=1)
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured fields stored as JSONB and queryable by key.",
    )
    trace_id: str | None = Field(default=None, max_length=128)

    @field_validator("timestamp")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("service", "host", "logger", "trace_id")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LogEntry(BaseModel):
    """A stored log record."""

    id: int
    timestamp: datetime
    received_at: datetime
    level: LogLevel
    service: str
    host: str | None
    logger: str | None
    message: str
    context: dict[str, Any]
    trace_id: str | None


class IngestResponse(BaseModel):
    """Result of an ingestion call."""

    accepted: int = Field(description="Number of records written.")
    ids: list[int] = Field(description="Identifiers of the written records, in submission order.")


class LogPage(BaseModel):
    """One page of query results, newest first."""

    items: list[LogEntry]
    count: int
    next_cursor: str | None = Field(
        default=None,
        description="Pass back as `cursor` to fetch the following page. Null when exhausted.",
    )


class LevelCount(BaseModel):
    level: LogLevel
    count: int


class ServiceCount(BaseModel):
    service: str
    count: int


class StatsResponse(BaseModel):
    """Aggregate counts over a time window."""

    since: datetime
    until: datetime
    total: int
    by_level: list[LevelCount]
    by_service: list[ServiceCount]


METRIC_NAMES: tuple[str, ...] = ("e2e", "network", "processing", "sender")


class MetricSummary(BaseModel):
    """One derived metric, summarised over a whole session.

    Percentiles cannot be averaged across windows, so the honest summary
    reports the *typical* window (median of that window's percentile) and the
    *worst* window separately. ``min_ns``/``max_ns``/``mean_ns`` are exact for
    the session: extremes compose, and the mean is count-weighted.
    """

    windows: int = Field(description="Snapshot windows that reported this metric.")
    samples: int = Field(description="Frames summarised, across all windows.")
    min_ns: int
    max_ns: int
    mean_ns: float = Field(description="Count-weighted mean. Exact for the session.")
    stddev_ns: float = Field(description="Pooled: within-window variance plus spread of means.")
    p50_typical_ns: int = Field(description="Median across windows of that window's p50.")
    p90_typical_ns: int
    p99_typical_ns: int
    p99_worst_ns: int = Field(description="Worst single window's p99.")


class ServiceStats(BaseModel):
    """Per-service accounting. Sequence numbers are per-recorder, so frame
    loss only means anything within one service."""

    service: str
    host: str | None
    windows: int
    rows_written: int = Field(description="Frames the recorder wrote.")
    samples_dropped: int = Field(description="Frames the recorder dropped: its ring was full.")
    snapshots_dropped: int
    seq_first: int | None
    seq_last: int | None
    frames_expected: int | None = Field(
        default=None, description="seq_last - seq_first + 1, when both are known."
    )
    frames_missing: int | None = Field(
        default=None,
        description="Expected minus written: frames that never reached the recorder at all.",
    )


class PtpSummary(BaseModel):
    """Whether cross-host timing can be trusted at all.

    ``e2e`` and ``network`` are differences between clocks on two hosts, so
    they measure clock offset rather than latency when PTP is not locked.
    """

    windows: int
    reliable_windows: int
    reliable_pct: float
    max_abs_offset_ns: int | None
    trustworthy: bool = Field(description="True when every window reported a reliable lock.")


class BudgetSplit(BaseModel):
    """Where the glass-to-glass time goes, as a share of the e2e mean.

    ``unaccounted_ns`` is e2e minus the three parts. It is not waste: the
    parts come from independent windows and PTP offset lands here too, so
    treat a small residual as noise and a large one as a clock problem.
    """

    sender_ns: float
    network_ns: float
    processing_ns: float
    unaccounted_ns: float
    total_ns: float


class SessionSummary(BaseModel):
    """One row in the session picker."""

    trace_id: str
    started_at: datetime
    ended_at: datetime
    duration_s: float
    windows: int
    services: list[str]
    hosts: list[str]
    rows_written: int
    samples_dropped: int
    ptp_reliable_pct: float
    e2e_p50_typical_ns: int | None = None
    e2e_p99_worst_ns: int | None = None


class SessionList(BaseModel):
    since: datetime
    until: datetime
    count: int
    items: list[SessionSummary]


class SessionDetail(BaseModel):
    """Everything the dashboard shows above the charts."""

    trace_id: str
    started_at: datetime
    ended_at: datetime
    duration_s: float
    windows: int
    services: list[str]
    hosts: list[str]
    interval_s: float | None
    metrics: dict[str, MetricSummary]
    by_service: list[ServiceStats]
    ptp: PtpSummary
    budget: BudgetSplit | None
    effective_rate_hz: float | None
    slo_threshold_ns: int
    slo_compliance_pct: float | None = Field(
        default=None, description="Share of windows whose p99 sits under the threshold."
    )
    p99_drift_ns_per_min: float | None = Field(
        default=None, description="Least-squares slope of window p99 over the session."
    )


class SessionTimeseries(BaseModel):
    """Columnar series, shaped for charting directly.

    Every list has the same length as ``t``. Nulls mark windows where a
    metric was absent rather than zero.
    """

    trace_id: str
    t: list[float] = Field(description="Unix seconds, ascending.")
    elapsed_s: list[float] = Field(description="Seconds since the session started.")
    service: list[str]
    e2e_p50_ns: list[float | None]
    e2e_p90_ns: list[float | None]
    e2e_p99_ns: list[float | None]
    e2e_mean_ns: list[float | None]
    e2e_min_ns: list[float | None]
    e2e_max_ns: list[float | None]
    sender_mean_ns: list[float | None]
    network_mean_ns: list[float | None]
    processing_mean_ns: list[float | None]
    ptp_offset_ns: list[float | None]
    ptp_reliable: list[bool]
    samples_delta: list[int]
    rows_written: list[int]


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str | None = None


def encode_cursor(timestamp: datetime, entry_id: int) -> str:
    """Encode a keyset position into an opaque cursor."""
    raw = f"{timestamp.astimezone(UTC).isoformat()}|{entry_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Decode a cursor produced by :func:`encode_cursor`.

    Raises:
        ValueError: if the cursor is not a well-formed position.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("malformed cursor") from exc

    timestamp_part, separator, id_part = raw.rpartition("|")
    if not separator:
        raise ValueError("malformed cursor")
    try:
        timestamp = datetime.fromisoformat(timestamp_part)
        entry_id = int(id_part)
    except ValueError as exc:
        raise ValueError("malformed cursor") from exc

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp, entry_id
