"""Data access for log entries. All SQL lives here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .schemas import (
    LevelCount,
    LogEntry,
    LogEntryCreate,
    LogLevel,
    ServiceCount,
)

_COLUMNS = '"timestamp", received_at, level, service, host, logger, message, context, trace_id'


@dataclass(slots=True)
class LogFilter:
    """Filters applied to a log query. Unset fields are ignored."""

    services: list[str] = field(default_factory=list)
    levels: list[LogLevel] = field(default_factory=list)
    min_level: LogLevel | None = None
    start: datetime | None = None
    end: datetime | None = None
    search: str | None = None
    trace_id: str | None = None
    host: str | None = None
    contains: dict[str, Any] | None = None


def _row_to_entry(row: asyncpg.Record) -> LogEntry:
    return LogEntry(
        id=row["id"],
        timestamp=row["timestamp"],
        received_at=row["received_at"],
        level=LogLevel(row["level"]),
        service=row["service"],
        host=row["host"],
        logger=row["logger"],
        message=row["message"],
        context=row["context"] or {},
        trace_id=row["trace_id"],
    )


class LogRepository:
    """Reads and writes log entries."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add_many(self, entries: list[LogEntryCreate]) -> list[int]:
        """Insert entries in one round trip. Returns the new ids in submission order."""
        if not entries:
            return []

        now = datetime.now(UTC)
        timestamps = [entry.timestamp or now for entry in entries]
        levels = [entry.level.value for entry in entries]
        severities = [entry.level.severity for entry in entries]
        services = [entry.service for entry in entries]
        hosts = [entry.host for entry in entries]
        loggers = [entry.logger for entry in entries]
        messages = [entry.message for entry in entries]
        contexts = [entry.context for entry in entries]
        trace_ids = [entry.trace_id for entry in entries]

        # A single multi-row INSERT: one round trip regardless of batch size.
        query = f"""
            INSERT INTO log_entries ({_COLUMNS}, severity)
            SELECT ts, now(), level, service, host, logger, message, context, trace_id, severity
            FROM unnest(
                $1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[],
                $6::text[], $7::jsonb[], $8::text[], $9::smallint[]
            ) WITH ORDINALITY AS t(
                ts, level, service, host, logger, message, context, trace_id, severity, ord
            )
            ORDER BY t.ord
            RETURNING id
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                query,
                timestamps,
                levels,
                services,
                hosts,
                loggers,
                messages,
                contexts,
                trace_ids,
                severities,
            )
        # Ids come from one sequence advanced in insertion order, so sorting
        # restores submission order without relying on RETURNING's row order.
        return sorted(row["id"] for row in rows)

    async def get(self, entry_id: int) -> LogEntry | None:
        query = f"SELECT id, {_COLUMNS} FROM log_entries WHERE id = $1"
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(query, entry_id)
        return _row_to_entry(row) if row else None

    async def query(
        self,
        filters: LogFilter,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[LogEntry]:
        """Return up to ``limit`` entries, newest first, after the keyset ``cursor``."""
        clauses, args = _build_clauses(filters)

        if cursor is not None:
            args.append(cursor[0])
            args.append(cursor[1])
            clauses.append(
                f'("timestamp", id) < (${len(args) - 1}::timestamptz, ${len(args)}::bigint)'
            )

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        query = f"""
            SELECT id, {_COLUMNS}
            FROM log_entries
            {where}
            ORDER BY "timestamp" DESC, id DESC
            LIMIT ${len(args)}
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, *args)
        return [_row_to_entry(row) for row in rows]

    async def stats(
        self,
        since: datetime,
        until: datetime,
        services: list[str] | None = None,
        service_limit: int = 20,
    ) -> tuple[int, list[LevelCount], list[ServiceCount]]:
        """Return total, per-level, and top-N per-service counts within a window."""
        filters = LogFilter(start=since, end=until, services=services or [])
        clauses, args = _build_clauses(filters)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        async with self._pool.acquire() as connection:
            level_rows = await connection.fetch(
                f"SELECT level, count(*) AS count FROM log_entries {where} GROUP BY level",
                *args,
            )
            service_rows = await connection.fetch(
                f"""
                SELECT service, count(*) AS count
                FROM log_entries {where}
                GROUP BY service
                ORDER BY count DESC, service ASC
                LIMIT ${len(args) + 1}
                """,
                *args,
                service_limit,
            )

        by_level = sorted(
            (LevelCount(level=LogLevel(row["level"]), count=row["count"]) for row in level_rows),
            key=lambda item: item.level.severity,
        )
        by_service = [
            ServiceCount(service=row["service"], count=row["count"]) for row in service_rows
        ]
        total = sum(item.count for item in by_level)
        return total, by_level, by_service

    async def count_before(self, before: datetime) -> int:
        """Count entries older than ``before``."""
        async with self._pool.acquire() as connection:
            return await connection.fetchval(
                'SELECT count(*) FROM log_entries WHERE "timestamp" < $1', before
            )

    async def purge(self, before: datetime) -> int:
        """Delete entries older than ``before``. Returns the number removed."""
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                'DELETE FROM log_entries WHERE "timestamp" < $1', before
            )
        # asyncpg returns a status string such as "DELETE 42".
        return int(result.rsplit(" ", 1)[-1])


def _build_clauses(filters: LogFilter) -> tuple[list[str], list[Any]]:
    """Translate a filter into SQL fragments and positional arguments."""
    clauses: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        clauses.append(fragment.format(n=len(args)))

    if filters.services:
        add("service = ANY(${n}::text[])", filters.services)
    if filters.levels:
        add("level = ANY(${n}::text[])", [level.value for level in filters.levels])
    if filters.min_level is not None:
        add("severity >= ${n}", filters.min_level.severity)
    if filters.start is not None:
        add('"timestamp" >= ${n}', filters.start)
    if filters.end is not None:
        add('"timestamp" <= ${n}', filters.end)
    if filters.search:
        add("search_vector @@ websearch_to_tsquery('english', ${n})", filters.search)
    if filters.trace_id:
        add("trace_id = ${n}", filters.trace_id)
    if filters.host:
        add("host = ${n}", filters.host)
    if filters.contains:
        add("context @> ${n}::jsonb", filters.contains)

    return clauses, args
