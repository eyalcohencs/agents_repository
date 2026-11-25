from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.db import create_db_engine, create_session_factory
from app.core.errors import register_error_handlers
from app.health.router import router as health_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application without import-time DB I/O."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else get_settings()
        engine = create_db_engine(resolved_settings)
        app.state.settings = resolved_settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            engine.dispose()

    resolved_env = (
        settings.app_env if settings is not None else get_settings().app_env
    )
    is_production = resolved_env == "production"

    app = FastAPI(
        title="OnTrack API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings

    register_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
