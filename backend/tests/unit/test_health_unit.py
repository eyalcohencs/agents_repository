from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.db import ping_database
from app.main import create_app


def _make_test_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        database_url="postgresql+psycopg://secret_user:top_secret_pw@db-internal:5432/ontrack",
        support_email="help@ontrack.example",
        network_layer_min_zoom=13,
        max_upload_mb=15,
        cookie_secure=True,
        upload_storage_dir="/var/private/uploads",
    )


def test_health_returns_200_when_database_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.health.router.ping_database", lambda _engine: True)
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.health.router.ping_database", lambda _engine: False)
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service temporarily unavailable.",
        "details": {"database": "unreachable"},
    }
    assert "top_secret_pw" not in response.text
    assert "db-internal" not in response.text


def test_public_config_returns_exact_non_secret_fields() -> None:
    app = create_app(_make_test_settings())

    with TestClient(app) as client:
        response = client.get("/api/config/public")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"support_email", "network_layer_min_zoom", "max_upload_mb"}
    assert body == {
        "support_email": "help@ontrack.example",
        "network_layer_min_zoom": 13,
        "max_upload_mb": 15,
    }
    raw = response.text
    for forbidden in (
        "database_url",
        "secret_user",
        "top_secret_pw",
        "db-internal",
        "cookie_secure",
        "upload_storage_dir",
        "/var/private/uploads",
    ):
        assert forbidden not in raw


def test_ping_database_executes_timeout_and_select_1() -> None:
    executed_sql: list[str] = []

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, stmt: object) -> None:
            executed_sql.append(str(stmt))

    fake_engine = SimpleNamespace(connect=lambda: _FakeConn())
    assert ping_database(fake_engine) is True  # type: ignore[arg-type]
    assert executed_sql == ["SET LOCAL statement_timeout = 2000", "SELECT 1"]


def test_ping_database_returns_false_on_sqlalchemy_error() -> None:
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = OperationalError("SELECT 1", {}, Exception("conn refused"))

    assert ping_database(fake_engine) is False


def test_production_app_disables_openapi_and_docs_endpoints() -> None:
    prod_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://ontrack_prod:S3cr3t!ProdPass99@db.internal:5432/ontrack",
        support_email="support@ontrack.example",
        cookie_secure=True,
    )
    prod_app = create_app(prod_settings)

    assert prod_app.docs_url is None
    assert prod_app.redoc_url is None
    assert prod_app.openapi_url is None

    with TestClient(prod_app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            resp = client.get(path)
            assert resp.status_code == 404
            assert resp.json()["code"] == "NOT_FOUND"


def test_non_production_app_exposes_openapi_schema() -> None:
    dev_app = create_app(_make_test_settings())

    assert dev_app.docs_url == "/docs"
    assert dev_app.redoc_url == "/redoc"
    assert dev_app.openapi_url == "/openapi.json"

