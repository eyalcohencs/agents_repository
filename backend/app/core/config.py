from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_DEV_DATABASE_URL = "postgresql+psycopg://ontrack:ontrack@localhost:5432/ontrack"
_DISALLOWED_PRODUCTION_DB_PASSWORDS = frozenset(
    {"ontrack", "postgres", "password", "changeme"}
)
ROUTING_FALLBACK_PROFILE_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_ROUTING_FALLBACK_PROFILE_RE = re.compile(ROUTING_FALLBACK_PROFILE_PATTERN)


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

    # T-007 POC-locked synthetic defaults: snapping / upload
    max_upload_points: int = Field(default=50_000, ge=1)
    default_point_simplification_distance_m: float = Field(
        default=20.0, gt=0.0, allow_inf_nan=False
    )
    default_node_snap_radius_m: float = Field(default=20.0, gt=0.0, allow_inf_nan=False)

    # T-007 POC-locked synthetic defaults: routing (pgr_dijkstra exactness lock)
    routing_algorithm: Literal["pgr_dijkstra"] = "pgr_dijkstra"
    routing_cost_mode: Literal["distance", "hops"] = "distance"
    routing_close_radius_m: float = Field(default=2000.0, gt=0.0, allow_inf_nan=False)
    routing_far_radius_m: float = Field(default=10_000.0, gt=0.0, allow_inf_nan=False)
    routing_fallback_profile: str = Field(
        default="cycling-mountain",
        pattern=ROUTING_FALLBACK_PROFILE_PATTERN,
    )
    routing_continuity_tolerance_m: float = Field(default=150.0, gt=0.0, allow_inf_nan=False)
    routing_provider_timeout_s: float = Field(default=10.0, gt=0.0, allow_inf_nan=False)
    routing_provider_max_retries: int = Field(default=2, ge=0, le=10)

    # T-007 POC-locked synthetic defaults: queue / worker (Procrastinate lock)
    queue_backend: Literal["procrastinate"] = "procrastinate"
    worker_lease_duration_s: int = Field(default=120, ge=1)
    worker_heartbeat_interval_s: int = Field(default=30, ge=1)
    worker_reconcile_interval_s: int = Field(default=60, ge=1)
    worker_max_attempts: int = Field(default=5, ge=1)
    worker_job_retention_days: int = Field(default=14, ge=1)

    @field_validator(
        "db_pool_size",
        "db_max_overflow",
        "db_statement_timeout_ms",
        "network_layer_min_zoom",
        "max_upload_mb",
        "max_upload_points",
        "default_point_simplification_distance_m",
        "default_node_snap_radius_m",
        "routing_close_radius_m",
        "routing_far_radius_m",
        "routing_continuity_tolerance_m",
        "routing_provider_timeout_s",
        "routing_provider_max_retries",
        "worker_lease_duration_s",
        "worker_heartbeat_interval_s",
        "worker_reconcile_interval_s",
        "worker_max_attempts",
        "worker_job_retention_days",
        mode="before",
    )
    @classmethod
    def reject_bool_for_numeric_settings(cls, value: object) -> object:
        """Reject Python bool values before Pydantic numeric coercion."""
        if isinstance(value, bool):
            raise ValueError("Boolean values are not allowed for numeric settings.")
        return value

    @field_validator(
        "default_point_simplification_distance_m",
        "default_node_snap_radius_m",
        "routing_close_radius_m",
        "routing_far_radius_m",
        "routing_continuity_tolerance_m",
        "routing_provider_timeout_s",
        mode="after",
    )
    @classmethod
    def validate_finite_positive_float(cls, value: float) -> float:
        """Ensure float settings are finite and strictly positive."""
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("Numeric float settings must be finite and greater than 0.")
        return value

    @field_validator("routing_fallback_profile", mode="after")
    @classmethod
    def validate_routing_fallback_profile(cls, value: str) -> str:
        """Ensure routing fallback profile strictly matches the safe token allow-pattern."""
        if not _ROUTING_FALLBACK_PROFILE_RE.fullmatch(value):
            raise ValueError(
                "ROUTING_FALLBACK_PROFILE must match ^[a-z0-9][a-z0-9_-]{0,63}$."
            )
        return value

    @model_validator(mode="after")
    def validate_poc_locked_settings(self) -> Settings:
        """Enforce cross-field invariants for T-007 POC-locked routing and worker settings."""
        if self.routing_far_radius_m < self.routing_close_radius_m:
            raise ValueError(
                "ROUTING_FAR_RADIUS_M must be greater than or equal to ROUTING_CLOSE_RADIUS_M."
            )
        if self.worker_heartbeat_interval_s * 2 > self.worker_lease_duration_s:
            raise ValueError(
                "WORKER_HEARTBEAT_INTERVAL_S * 2 must be less than or equal to "
                "WORKER_LEASE_DURATION_S."
            )
        return self

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
