"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from .config import Settings
from .db import Database
from .repository import LogRepository


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_repository(
    database: Annotated[Database, Depends(get_database)],
) -> LogRepository:
    return LogRepository(database.pool)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RepositoryDep = Annotated[LogRepository, Depends(get_repository)]
