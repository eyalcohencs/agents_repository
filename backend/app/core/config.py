from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_DEV_DATABASE_URL = "postgresql+psycopg://ontrack:ontrack@localhost:5432/ontrack"
_DISALLOWED_PRODUCTION_DB_PASSWORDS = frozenset(
    {"ontrack", "postgres", "password", "changeme"}
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables and optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = DEFAULT_DEV_DATABASE_URL
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=15000, ge=100, le=300000)
    upload_storage_dir: str = "/data/uploads"
    support_email: str = "support@ontrack.local"
    network_layer_min_zoom: int = Field(default=12, ge=1, le=22)
    max_upload_mb: int = Field(default=10, ge=1, le=100)
    cookie_secure: bool = False
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production_settings(self) -> Settings:
        email = self.support_email.strip()
        self.support_email = email

        if self.app_env == "production":
            local, sep, domain = email.partition("@")
            if (
                not sep
                or not local
                or not domain
                or "@" in domain
                or "." not in domain
                or domain.startswith(".")
                or domain.endswith(".")
                or domain.lower().endswith(".local")
                or any(ch.isspace() for ch in email)
            ):
                raise ValueError(
                    "SUPPORT_EMAIL must be a valid non-.local address in production."
                )

            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be True in production.")

            if self.database_url.strip() == DEFAULT_DEV_DATABASE_URL:
                raise ValueError(
                    "DATABASE_URL must not use the default development credentials in production."
                )
            try:
                parsed_url = make_url(self.database_url)
            except ArgumentError as exc:
                raise ValueError(
                    "DATABASE_URL must be a valid SQLAlchemy URL in production."
                ) from exc

            password = (parsed_url.password or "").strip()
            if not password or password.lower() in _DISALLOWED_PRODUCTION_DB_PASSWORDS:
                raise ValueError(
                    "DATABASE_URL must specify a non-default password in production."
                )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
