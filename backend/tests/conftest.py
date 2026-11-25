from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
PGROUTING_IMAGE = "pgrouting/pgrouting:16-3.5-3.7"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Ensure cached Settings do not leak across tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def to_psycopg_url(raw_url: str) -> str:
    """Normalize a PostgreSQL URL to use the SQLAlchemy psycopg v3 driver."""
    if raw_url.startswith("postgresql+psycopg://"):
        return raw_url
    if raw_url.startswith("postgresql+psycopg2://"):
        return raw_url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


def build_alembic_config(database_url: str) -> Config:
    """Build an Alembic Config bound to the given database URL."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture(scope="session")
def postgres_db_url() -> Generator[str, None, None]:
    """Spin up a real PostGIS + pgRouting container for integration tests."""
    with PostgresContainer(PGROUTING_IMAGE) as container:
        yield to_psycopg_url(container.get_connection_url())
