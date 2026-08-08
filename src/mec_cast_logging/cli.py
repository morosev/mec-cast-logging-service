"""Command line entry point: ``mec-cast-logs``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta

from .config import Settings, get_settings
from .db import Database
from .repository import LogRepository

logger = logging.getLogger("mec_cast_logging.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mec-cast-logs",
        description="Run and maintain the mec-cast logging service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the HTTP service.")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Reload on source changes.")
    serve.add_argument("--workers", type=int, default=1)

    subparsers.add_parser("migrate", help="Apply pending database migrations and exit.")

    purge = subparsers.add_parser("purge", help="Delete entries older than the retention window.")
    purge.add_argument(
        "--days",
        type=int,
        default=None,
        help="Retention window in days. Defaults to MECLOG_RETENTION_DAYS.",
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many entries would be deleted without deleting them.",
    )

    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(levelname)-8s %(message)s")

    if args.command == "serve":
        return _serve(args, settings)
    if args.command == "migrate":
        return asyncio.run(_migrate(settings))
    if args.command == "purge":
        return asyncio.run(_purge(settings, args.days, args.dry_run))
    return 1


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run(
        "mec_cast_logging.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level=settings.log_level.lower(),
    )
    return 0


async def _migrate(settings: Settings) -> int:
    database = Database(settings)
    await database.connect()
    try:
        applied = await database.migrate()
    finally:
        await database.disconnect()

    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Schema is up to date.")
    return 0


async def _purge(settings: Settings, days: int | None, dry_run: bool) -> int:
    window = days if days is not None else settings.retention_days
    if window < 1:
        print("Retention window must be at least 1 day.", file=sys.stderr)
        return 2

    cutoff = datetime.now(UTC) - timedelta(days=window)
    database = Database(settings)
    await database.connect()
    try:
        repository = LogRepository(database.pool)
        if dry_run:
            pending = await repository.count_before(cutoff)
            print(f"Would delete {pending} entries older than {cutoff.isoformat()}.")
            return 0
        deleted = await repository.purge(cutoff)
    finally:
        await database.disconnect()

    print(f"Deleted {deleted} entries older than {cutoff.isoformat()}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
