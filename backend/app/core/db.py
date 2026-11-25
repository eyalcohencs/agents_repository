from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint naming conventions."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_db_engine(settings: Settings) -> Engine:
    """Create a SQLAlchemy 2 engine configured with pool pre-ping and statement timeout."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={
            "connect_timeout": 5,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a SQLAlchemy 2 session factory."""
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


def get_db(request: Request) -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session from application state."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session


def ping_database(engine: Engine) -> bool:
    """Perform a lightweight database liveness check with a 2-second statement timeout."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
