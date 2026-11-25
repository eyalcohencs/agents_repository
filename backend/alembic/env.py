from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

POSTGIS_SYSTEM_TABLES = frozenset(
    {
        "spatial_ref_sys",
        "geography_columns",
        "geometry_columns",
        "raster_columns",
        "raster_overviews",
    }
)


def _get_database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return os.environ.get("DATABASE_URL") or get_settings().database_url


def include_object(
    object_: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    return not (type_ == "table" and name in POSTGIS_SYSTEM_TABLES)


def run_migrations_offline() -> None:
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _get_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
