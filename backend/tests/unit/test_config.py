from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_DEV_DATABASE_URL, Settings, get_settings

_PROD_DATABASE_URL = "postgresql+psycopg://ontrack_prod:S3cr3t!ProdPass99@db.internal:5432/ontrack"

_CONFIG_ENV_KEYS = (
    "APP_ENV",
    "DATABASE_URL",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_STATEMENT_TIMEOUT_MS",
    "UPLOAD_STORAGE_DIR",
    "SUPPORT_EMAIL",
    "NETWORK_LAYER_MIN_ZOOM",
    "MAX_UPLOAD_MB",
    "COOKIE_SECURE",
    "LOG_LEVEL",
    "MAX_UPLOAD_POINTS",
    "DEFAULT_POINT_SIMPLIFICATION_DISTANCE_M",
    "DEFAULT_NODE_SNAP_RADIUS_M",
    "ROUTING_ALGORITHM",
    "ROUTING_COST_MODE",
    "ROUTING_CLOSE_RADIUS_M",
    "ROUTING_FAR_RADIUS_M",
    "ROUTING_FALLBACK_PROFILE",
    "ROUTING_CONTINUITY_TOLERANCE_M",
    "ROUTING_PROVIDER_TIMEOUT_S",
    "ROUTING_PROVIDER_MAX_RETRIES",
    "QUEUE_BACKEND",
    "WORKER_LEASE_DURATION_S",
    "WORKER_HEARTBEAT_INTERVAL_S",
    "WORKER_RECONCILE_INTERVAL_S",
    "WORKER_MAX_ATTEMPTS",
    "WORKER_JOB_RETENTION_DAYS",
)


@pytest.fixture
def clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove application config env vars so tests verify clean defaults/overrides."""
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_defaults(clean_config_env: None) -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.database_url == DEFAULT_DEV_DATABASE_URL
    assert settings.db_pool_size == 5
    assert settings.db_max_overflow == 10
    assert settings.db_statement_timeout_ms == 15000
    assert settings.upload_storage_dir == "/data/uploads"
    assert settings.support_email == "support@ontrack.local"
    assert settings.network_layer_min_zoom == 12
    assert settings.max_upload_mb == 10
    assert settings.cookie_secure is False
    assert settings.log_level == "INFO"


def test_settings_env_overrides_and_extra_ignored(
    clean_config_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/testdb")
    monkeypatch.setenv("DB_POOL_SIZE", "8")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "16")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "20000")
    monkeypatch.setenv("UPLOAD_STORAGE_DIR", "/tmp/ontrack-uploads")
    monkeypatch.setenv("SUPPORT_EMAIL", "ops@ontrack.example")
    monkeypatch.setenv("NETWORK_LAYER_MIN_ZOOM", "14")
    monkeypatch.setenv("MAX_UPLOAD_MB", "25")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("UNRELATED_EXTRA_VAR", "ignored")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/testdb"
    assert settings.db_pool_size == 8
    assert settings.db_max_overflow == 16
    assert settings.db_statement_timeout_ms == 20000
    assert settings.upload_storage_dir == "/tmp/ontrack-uploads"
    assert settings.support_email == "ops@ontrack.example"
    assert settings.network_layer_min_zoom == 14
    assert settings.max_upload_mb == 25
    assert settings.cookie_secure is True
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize("zoom", [1, 12, 22])
def test_network_layer_min_zoom_valid_bounds(clean_config_env: None, zoom: int) -> None:
    settings = Settings(_env_file=None, network_layer_min_zoom=zoom)
    assert settings.network_layer_min_zoom == zoom


@pytest.mark.parametrize("zoom", [0, -1, 23, 100])
def test_network_layer_min_zoom_invalid_bounds(clean_config_env: None, zoom: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, network_layer_min_zoom=zoom)


@pytest.mark.parametrize("max_mb", [1, 10, 100])
def test_max_upload_mb_valid_bounds(clean_config_env: None, max_mb: int) -> None:
    settings = Settings(_env_file=None, max_upload_mb=max_mb)
    assert settings.max_upload_mb == max_mb


@pytest.mark.parametrize("max_mb", [0, -5, 101])
def test_max_upload_mb_invalid_bounds(clean_config_env: None, max_mb: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_upload_mb=max_mb)


def test_invalid_app_env_rejected(clean_config_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="staging")


def test_production_requires_cookie_secure(clean_config_env: None) -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=_PROD_DATABASE_URL,
            support_email="support@ontrack.example",
            cookie_secure=False,
        )


@pytest.mark.parametrize(
    "bad_email",
    [
        "",
        "   ",
        "not-an-email",
        "@",
        "a@b",
        "@ontrack.example",
        "support@",
        "support@.ontrack.example",
        "support@ontrack.example.",
        "support@a@b.example",
        "bad email@ontrack.example",
        "support@ontrack.local",
        "SUPPORT@ONTRACK.LOCAL",
        "admin@dev.local ",
    ],
)
def test_production_rejects_empty_malformed_or_local_support_email(
    clean_config_env: None,
    bad_email: str,
) -> None:
    with pytest.raises(ValidationError, match="SUPPORT_EMAIL"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=_PROD_DATABASE_URL,
            support_email=bad_email,
            cookie_secure=True,
        )


def test_production_rejects_default_dev_database_url(clean_config_env: None) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=DEFAULT_DEV_DATABASE_URL,
            support_email="support@ontrack.example",
            cookie_secure=True,
        )


@pytest.mark.parametrize(
    "bad_db_url",
    [
        "not-a-valid-url",
        "postgresql+psycopg://ontrack@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:ontrack@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:ONTRACK@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:postgres@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:password@db.internal:5432/ontrack",
        "postgresql+psycopg://ontrack:changeme@db.internal:5432/ontrack",
    ],
)
def test_production_rejects_missing_invalid_or_weak_db_password(
    clean_config_env: None,
    bad_db_url: str,
) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=bad_db_url,
            support_email="support@ontrack.example",
            cookie_secure=True,
        )


def test_production_accepts_valid_settings_and_strips_support_email(
    clean_config_env: None,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=_PROD_DATABASE_URL,
        support_email="  support@ontrack.example  ",
        cookie_secure=True,
    )
    assert settings.app_env == "production"
    assert settings.cookie_secure is True
    assert settings.database_url == _PROD_DATABASE_URL
    assert settings.support_email == "support@ontrack.example"


def test_get_settings_caches_and_resets(
    clean_config_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPPORT_EMAIL", "first@ontrack.example")
    first = get_settings()
    monkeypatch.setenv("SUPPORT_EMAIL", "second@ontrack.example")
    second = get_settings()

    assert first is second
    assert second.support_email == "first@ontrack.example"

    get_settings.cache_clear()
    third = get_settings()
    assert third is not first
    assert third.support_email == "second@ontrack.example"


# ---------------------------------------------------------------------------
# T-007 POC-locked synthetic defaults & cross-field validators
# ---------------------------------------------------------------------------


def test_t007_poc_locked_defaults(clean_config_env: None) -> None:
    settings = Settings(_env_file=None)

    # Snapping / upload defaults
    assert settings.max_upload_points == 50_000
    assert settings.default_point_simplification_distance_m == 20.0
    assert settings.default_node_snap_radius_m == 20.0

    # Routing defaults (pgr_dijkstra exactness lock)
    assert settings.routing_algorithm == "pgr_dijkstra"
    assert settings.routing_cost_mode == "distance"
    assert settings.routing_close_radius_m == 2000.0
    assert settings.routing_far_radius_m == 10_000.0
    assert settings.routing_fallback_profile == "cycling-mountain"
    assert settings.routing_continuity_tolerance_m == 150.0
    assert settings.routing_provider_timeout_s == 10.0
    assert settings.routing_provider_max_retries == 2

    # Queue / worker defaults (Procrastinate lock)
    assert settings.queue_backend == "procrastinate"
    assert settings.worker_lease_duration_s == 120
    assert settings.worker_heartbeat_interval_s == 30
    assert settings.worker_reconcile_interval_s == 60
    assert settings.worker_max_attempts == 5
    assert settings.worker_job_retention_days == 14


def test_t007_poc_locked_env_overrides(
    clean_config_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_POINTS", "75000")
    monkeypatch.setenv("DEFAULT_POINT_SIMPLIFICATION_DISTANCE_M", "20.0")
    monkeypatch.setenv("DEFAULT_NODE_SNAP_RADIUS_M", "10.0")
    monkeypatch.setenv("ROUTING_ALGORITHM", "pgr_dijkstra")
    monkeypatch.setenv("ROUTING_COST_MODE", "hops")
    monkeypatch.setenv("ROUTING_CLOSE_RADIUS_M", "1500.5")
    monkeypatch.setenv("ROUTING_FAR_RADIUS_M", "8000.0")
    monkeypatch.setenv("ROUTING_FALLBACK_PROFILE", "foot_hiking-2")
    monkeypatch.setenv("ROUTING_CONTINUITY_TOLERANCE_M", "120.0")
    monkeypatch.setenv("ROUTING_PROVIDER_TIMEOUT_S", "15.5")
    monkeypatch.setenv("ROUTING_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("QUEUE_BACKEND", "procrastinate")
    monkeypatch.setenv("WORKER_LEASE_DURATION_S", "180")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_S", "45")
    monkeypatch.setenv("WORKER_RECONCILE_INTERVAL_S", "30")
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("WORKER_JOB_RETENTION_DAYS", "30")

    settings = Settings(_env_file=None)

    assert settings.max_upload_points == 75_000
    assert settings.default_point_simplification_distance_m == 20.0
    assert settings.default_node_snap_radius_m == 10.0
    assert settings.routing_algorithm == "pgr_dijkstra"
    assert settings.routing_cost_mode == "hops"
    assert settings.routing_close_radius_m == 1500.5
    assert settings.routing_far_radius_m == 8000.0
    assert settings.routing_fallback_profile == "foot_hiking-2"
    assert settings.routing_continuity_tolerance_m == 120.0
    assert settings.routing_provider_timeout_s == 15.5
    assert settings.routing_provider_max_retries == 0
    assert settings.queue_backend == "procrastinate"
    assert settings.worker_lease_duration_s == 180
    assert settings.worker_heartbeat_interval_s == 45
    assert settings.worker_reconcile_interval_s == 30
    assert settings.worker_max_attempts == 3
    assert settings.worker_job_retention_days == 30


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("routing_algorithm", "pgr_bdDijkstra"),
        ("routing_algorithm", "astar"),
        ("routing_cost_mode", "time"),
        ("queue_backend", "celery"),
        ("queue_backend", "skip_locked"),
    ],
)
def test_t007_literal_locks_reject_unsupported_values(
    clean_config_env: None,
    field_name: str,
    bad_value: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: bad_value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        # Positive integer fields & bool rejection
        ("max_upload_points", 0),
        ("max_upload_points", -10),
        ("max_upload_points", True),
        ("max_upload_points", False),
        ("routing_provider_max_retries", -1),
        ("routing_provider_max_retries", 11),
        ("routing_provider_max_retries", True),
        ("routing_provider_max_retries", False),
        ("worker_lease_duration_s", 0),
        ("worker_lease_duration_s", -5),
        ("worker_lease_duration_s", True),
        ("worker_heartbeat_interval_s", 0),
        ("worker_heartbeat_interval_s", -1),
        ("worker_heartbeat_interval_s", False),
        ("worker_reconcile_interval_s", 0),
        ("worker_reconcile_interval_s", -10),
        ("worker_reconcile_interval_s", True),
        ("worker_max_attempts", 0),
        ("worker_max_attempts", -1),
        ("worker_max_attempts", True),
        ("worker_job_retention_days", 0),
        ("worker_job_retention_days", -7),
        ("worker_job_retention_days", False),
        # Positive finite float fields & bool/NaN/Inf rejection
        ("default_point_simplification_distance_m", 0.0),
        ("default_point_simplification_distance_m", -1.0),
        ("default_point_simplification_distance_m", float("nan")),
        ("default_point_simplification_distance_m", float("inf")),
        ("default_point_simplification_distance_m", float("-inf")),
        ("default_point_simplification_distance_m", True),
        ("default_node_snap_radius_m", 0.0),
        ("default_node_snap_radius_m", -5.0),
        ("default_node_snap_radius_m", float("nan")),
        ("default_node_snap_radius_m", float("inf")),
        ("default_node_snap_radius_m", False),
        ("routing_close_radius_m", 0.0),
        ("routing_close_radius_m", -100.0),
        ("routing_close_radius_m", float("nan")),
        ("routing_close_radius_m", float("inf")),
        ("routing_close_radius_m", True),
        ("routing_far_radius_m", 0.0),
        ("routing_far_radius_m", -100.0),
        ("routing_far_radius_m", float("nan")),
        ("routing_far_radius_m", float("inf")),
        ("routing_far_radius_m", False),
        ("routing_continuity_tolerance_m", 0.0),
        ("routing_continuity_tolerance_m", -10.0),
        ("routing_continuity_tolerance_m", float("nan")),
        ("routing_continuity_tolerance_m", float("inf")),
        ("routing_continuity_tolerance_m", True),
        ("routing_provider_timeout_s", 0.0),
        ("routing_provider_timeout_s", -2.0),
        ("routing_provider_timeout_s", float("nan")),
        ("routing_provider_timeout_s", float("inf")),
        ("routing_provider_timeout_s", False),
    ],
)
def test_t007_numeric_bounds_reject_invalid_non_finite_and_bool(
    clean_config_env: None,
    field_name: str,
    bad_value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: bad_value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "valid_profile",
    ["cycling-mountain", "mtb", "foot_hiking-2", "0a_b-c", "a" * 64],
)
def test_t007_routing_fallback_profile_accepts_safe_tokens(
    clean_config_env: None,
    valid_profile: str,
) -> None:
    settings = Settings(
        _env_file=None,
        routing_fallback_profile=valid_profile,
    )
    assert settings.routing_fallback_profile == valid_profile


@pytest.mark.parametrize(
    "bad_profile",
    [
        "",
        "-leading-dash",
        "_leading_underscore",
        "UpperCase",
        "cycling mountain",
        "../traversal",
        "profile;drop",
        "a" * 65,
        "cycling-mountain\n",
    ],
)
def test_t007_routing_fallback_profile_rejects_unsafe_tokens(
    clean_config_env: None,
    bad_profile: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            routing_fallback_profile=bad_profile,
        )


def test_t007_cross_field_routing_radii_validator(clean_config_env: None) -> None:
    # Boundary: far == close is valid
    equal = Settings(
        _env_file=None,
        routing_close_radius_m=3000.0,
        routing_far_radius_m=3000.0,
    )
    assert equal.routing_far_radius_m == equal.routing_close_radius_m == 3000.0

    # Invalid: far < close
    with pytest.raises(ValidationError, match="ROUTING_FAR_RADIUS_M"):
        Settings(
            _env_file=None,
            routing_close_radius_m=5000.0,
            routing_far_radius_m=4999.9,
        )


def test_t007_cross_field_worker_heartbeat_lease_validator(clean_config_env: None) -> None:
    # Boundary: heartbeat * 2 == lease is valid
    boundary = Settings(
        _env_file=None,
        worker_lease_duration_s=120,
        worker_heartbeat_interval_s=60,
    )
    assert boundary.worker_heartbeat_interval_s == 60
    assert boundary.worker_lease_duration_s == 120

    # Invalid: heartbeat * 2 > lease
    with pytest.raises(ValidationError, match="WORKER_HEARTBEAT_INTERVAL_S"):
        Settings(
            _env_file=None,
            worker_lease_duration_s=120,
            worker_heartbeat_interval_s=61,
        )
