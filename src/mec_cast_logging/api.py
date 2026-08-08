"""HTTP routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status

from . import __version__
from .dependencies import DatabaseDep, RepositoryDep, SettingsDep
from .repository import LogFilter
from .schemas import (
    HealthResponse,
    IngestResponse,
    LogEntry,
    LogEntryCreate,
    LogLevel,
    LogPage,
    StatsResponse,
    decode_cursor,
    encode_cursor,
)

health_router = APIRouter(tags=["health"])
logs_router = APIRouter(tags=["logs"])


@health_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up. Does not touch the database."""
    return HealthResponse(status="ok", version=__version__)


@health_router.get("/health/ready", response_model=HealthResponse)
async def readiness(database: DatabaseDep, response: Response) -> HealthResponse:
    """Readiness: the process is up *and* the database answers."""
    reachable = await database.ping()
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if reachable else "degraded",
        version=__version__,
        database="up" if reachable else "down",
    )


@logs_router.post(
    "/logs",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest one entry or a batch",
)
async def ingest_logs(
    payload: LogEntryCreate | list[LogEntryCreate],
    repository: RepositoryDep,
    settings: SettingsDep,
) -> IngestResponse:
    entries = payload if isinstance(payload, list) else [payload]

    if not entries:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Submit at least one log entry.",
        )
    if len(entries) > settings.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch of {len(entries)} exceeds the limit of {settings.max_batch_size}.",
        )
    for index, entry in enumerate(entries):
        if len(entry.message) > settings.max_message_length:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Entry {index} has a {len(entry.message)} character message, "
                    f"over the limit of {settings.max_message_length}."
                ),
            )

    ids = await repository.add_many(entries)
    return IngestResponse(accepted=len(ids), ids=ids)


@logs_router.get("/logs", response_model=LogPage, summary="Query logs, newest first")
async def query_logs(
    repository: RepositoryDep,
    settings: SettingsDep,
    service: Annotated[list[str], Query(description="Repeatable. Matches any of the given.")] = [],  # noqa: B006
    level: Annotated[list[LogLevel], Query(description="Repeatable exact-level filter.")] = [],  # noqa: B006
    min_level: Annotated[LogLevel | None, Query(description="Severity floor.")] = None,
    start: Annotated[datetime | None, Query(description="Inclusive lower bound.")] = None,
    end: Annotated[datetime | None, Query(description="Inclusive upper bound.")] = None,
    q: Annotated[str | None, Query(description="Full-text search over the message.")] = None,
    trace_id: Annotated[str | None, Query()] = None,
    host: Annotated[str | None, Query()] = None,
    contains: Annotated[
        str | None, Query(description='JSON object the context must contain, e.g. {"user":42}.')
    ] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: Annotated[str | None, Query(description="`next_cursor` from a previous page.")] = None,
) -> LogPage:
    page_size = min(limit or settings.default_page_size, settings.max_page_size)

    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`start` must not be after `end`.",
        )

    keyset = None
    if cursor:
        try:
            keyset = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid cursor.",
            ) from None

    filters = LogFilter(
        services=service,
        levels=level,
        min_level=min_level,
        start=_as_utc(start),
        end=_as_utc(end),
        search=q,
        trace_id=trace_id,
        host=host,
        contains=_parse_contains(contains),
    )

    # Fetch one extra row to learn whether another page exists.
    rows = await repository.query(filters, limit=page_size + 1, cursor=keyset)
    has_more = len(rows) > page_size
    items = rows[:page_size]
    next_cursor = encode_cursor(items[-1].timestamp, items[-1].id) if has_more and items else None

    return LogPage(items=items, count=len(items), next_cursor=next_cursor)


@logs_router.get("/logs/{entry_id}", response_model=LogEntry, summary="Fetch one entry")
async def get_log(entry_id: int, repository: RepositoryDep) -> LogEntry:
    entry = await repository.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Log entry not found.")
    return entry


@logs_router.get("/stats", response_model=StatsResponse, summary="Counts over a window")
async def stats(
    repository: RepositoryDep,
    service: Annotated[list[str], Query()] = [],  # noqa: B006
    since: Annotated[datetime | None, Query(description="Defaults to 24 hours ago.")] = None,
    until: Annotated[datetime | None, Query(description="Defaults to now.")] = None,
) -> StatsResponse:
    window_end = _as_utc(until) or datetime.now(UTC)
    window_start = _as_utc(since) or window_end - timedelta(hours=24)
    if window_start > window_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`since` must not be after `until`.",
        )

    total, by_level, by_service = await repository.stats(
        since=window_start, until=window_end, services=service
    )
    return StatsResponse(
        since=window_start,
        until=window_end,
        total=total,
        by_level=by_level,
        by_service=by_service,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_contains(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`contains` must be valid JSON.",
        ) from None
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`contains` must be a JSON object.",
        )
    return parsed
