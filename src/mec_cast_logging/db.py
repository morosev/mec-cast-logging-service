"""Connection pooling and schema migrations."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg

from .config import Settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def _init_connection(connection: asyncpg.Connection) -> None:
    """Decode JSONB columns to dicts instead of raw strings."""
    await connection.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class Database:
    """Owns the asyncpg pool for the process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database pool is not connected")
        return self._pool

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.database_url,
            min_size=self._settings.db_pool_min_size,
            max_size=self._settings.db_pool_max_size,
            command_timeout=self._settings.db_command_timeout,
            init=_init_connection,
        )

    async def disconnect(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def ping(self) -> bool:
        """Return True if the database answers a trivial query."""
        try:
            async with self.pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logger.warning("database ping failed", exc_info=True)
            return False
        return True

    async def migrate(self) -> list[str]:
        """Apply any migration files not yet recorded. Returns the names applied."""
        async with self.pool.acquire() as connection:
            await connection.execute(_SCHEMA_VERSION_DDL)
            applied = {
                row["name"] for row in await connection.fetch("SELECT name FROM schema_migrations")
            }

            newly_applied: list[str] = []
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                # One transaction per migration: a failure leaves earlier ones intact.
                async with connection.transaction():
                    await connection.execute(path.read_text(encoding="utf-8"))
                    await connection.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
                    )
                logger.info("applied migration %s", path.name)
                newly_applied.append(path.name)

        return newly_applied
