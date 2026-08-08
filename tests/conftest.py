"""Shared fixtures.

Integration tests need a reachable PostgreSQL. Point `MECLOG_TEST_DATABASE_URL`
(or `MECLOG_DATABASE_URL`) at a throwaway database; the whole integration module
is skipped when neither is reachable. Every test runs inside a transaction that
is rolled back, so the database is left as it was found.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from mec_cast_logging.config import Settings
from mec_cast_logging.db import Database


def _test_database_url() -> str | None:
    return os.environ.get("MECLOG_TEST_DATABASE_URL") or os.environ.get("MECLOG_DATABASE_URL")


@pytest.fixture(scope="session")
def settings() -> Settings:
    url = _test_database_url()
    if url is None:
        pytest.skip("no test database configured (set MECLOG_TEST_DATABASE_URL)")
    return Settings(database_url=url, auto_migrate=False, max_batch_size=10, default_page_size=5)


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    try:
        await db.connect()
    except OSError as exc:
        pytest.skip(f"test database unreachable: {exc}")

    await db.migrate()
    try:
        yield db
    finally:
        async with db.pool.acquire() as connection:
            await connection.execute("TRUNCATE log_entries RESTART IDENTITY")
        await db.disconnect()
