"""Application factory and lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .api import health_router, logs_router
from .config import Settings, get_settings
from .db import Database

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database: Database = app.state.database
        await database.connect()
        if settings.auto_migrate:
            applied = await database.migrate()
            if applied:
                logger.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
        try:
            yield
        finally:
            await database.disconnect()

    app = FastAPI(
        title="mec-cast logging service",
        description=(
            "Collects, stores, and queries logs from mec-cast applications. "
            "Ingest over HTTP, store in PostgreSQL, query with filters and full-text search."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = Database(settings)

    app.include_router(health_router)
    app.include_router(logs_router, prefix=settings.api_prefix)

    @app.exception_handler(asyncpg.PostgresError)
    async def _database_error(request: Request, exc: asyncpg.PostgresError) -> JSONResponse:
        logger.exception("database error handling %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "The log store is unavailable. Retry shortly."},
        )

    return app


app = create_app()
