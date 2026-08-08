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
