"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings. Every field maps to a ``MECLOG_``-prefixed env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MECLOG_",
        extra="ignore",
    )

    database_url: str = "postgresql://postgres:postgres@localhost:5432/mec_cast_logs"
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    db_command_timeout: float = Field(default=30.0, gt=0)

    auto_migrate: bool = True

    max_batch_size: int = Field(default=500, ge=1)
    max_message_length: int = Field(default=65536, ge=1)

    default_page_size: int = Field(default=100, ge=1)
    max_page_size: int = Field(default=1000, ge=1)

    retention_days: int = Field(default=30, ge=1)

    api_prefix: str = "/api/v1"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
