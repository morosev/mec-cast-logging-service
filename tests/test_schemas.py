"""Unit tests for validation and cursor encoding. No database required."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mec_cast_logging.schemas import (
    LogEntryCreate,
    LogLevel,
    decode_cursor,
    encode_cursor,
)


def test_severity_is_ordered() -> None:
    levels = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL]
    severities = [level.severity for level in levels]
    assert severities == sorted(severities)
    assert len(set(severities)) == len(severities)


def test_naive_timestamp_is_treated_as_utc() -> None:
    entry = LogEntryCreate(
        level=LogLevel.INFO,
        service="ingest",
        message="hello",
        timestamp=datetime(2026, 1, 2, 3, 4, 5),
    )
    assert entry.timestamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_aware_timestamp_is_normalised_to_utc() -> None:
    entry = LogEntryCreate(
        level=LogLevel.INFO,
        service="ingest",
        message="hello",
        timestamp="2026-01-02T05:04:05+02:00",
    )
    assert entry.timestamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_blank_optional_strings_become_none() -> None:
    entry = LogEntryCreate(level=LogLevel.INFO, service="ingest", message="hi", host="   ")
    assert entry.host is None


def test_empty_service_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogEntryCreate(level=LogLevel.INFO, service="", message="hi")


def test_empty_message_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogEntryCreate(level=LogLevel.INFO, service="ingest", message="")


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogEntryCreate(level="TRACE", service="ingest", message="hi")


def test_unknown_top_level_field_is_rejected() -> None:
    # Extras belong in `context`, so a typo surfaces instead of being dropped.
    with pytest.raises(ValidationError):
        LogEntryCreate(level=LogLevel.INFO, service="ingest", message="hi", user_id=7)


def test_cursor_round_trips() -> None:
    moment = datetime(2026, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    timestamp, entry_id = decode_cursor(encode_cursor(moment, 4321))
    assert timestamp == moment
    assert entry_id == 4321


def test_cursor_has_no_padding_characters() -> None:
    cursor = encode_cursor(datetime.now(UTC), 1)
    assert "=" not in cursor


@pytest.mark.parametrize("bad", ["", "!!!", "bm90LWEtY3Vyc29y", "MjAyNi0wMS0wMXxub3QtYW4taW50"])
def test_malformed_cursors_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_cursor(bad)
