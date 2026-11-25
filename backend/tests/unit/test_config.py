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
)


@pytest.fixture
def clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove application config env vars so tests verify clean defaults/overrides."""
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_defaults(clean_config_env: None) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

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

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

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
    settings = Settings(_env_file=None, network_layer_min_zoom=zoom)  # type: ignore[call-arg]
    assert settings.network_layer_min_zoom == zoom


@pytest.mark.parametrize("zoom", [0, -1, 23, 100])
def test_network_layer_min_zoom_invalid_bounds(clean_config_env: None, zoom: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, network_layer_min_zoom=zoom)  # type: ignore[call-arg]


@pytest.mark.parametrize("max_mb", [1, 10, 100])
def test_max_upload_mb_valid_bounds(clean_config_env: None, max_mb: int) -> None:
    settings = Settings(_env_file=None, max_upload_mb=max_mb)  # type: ignore[call-arg]
    assert settings.max_upload_mb == max_mb


@pytest.mark.parametrize("max_mb", [0, -5, 101])
def test_max_upload_mb_invalid_bounds(clean_config_env: None, max_mb: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_upload_mb=max_mb)  # type: ignore[call-arg]


def test_invalid_app_env_rejected(clean_config_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="staging")  # type: ignore[call-arg,arg-type]


def test_production_requires_cookie_secure(clean_config_env: None) -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(  # type: ignore[call-arg]
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
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            database_url=_PROD_DATABASE_URL,
            support_email=bad_email,
            cookie_secure=True,
        )


def test_production_rejects_default_dev_database_url(clean_config_env: None) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(  # type: ignore[call-arg]
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
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            database_url=bad_db_url,
            support_email="support@ontrack.example",
            cookie_secure=True,
        )


def test_production_accepts_valid_settings_and_strips_support_email(
    clean_config_env: None,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
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
