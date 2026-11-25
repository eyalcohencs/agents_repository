from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings, get_settings
from app.core.db import ping_database
from app.core.errors import AppError, ErrorResponse
from app.health.schemas import HealthResponse, PublicConfigResponse

router = APIRouter(prefix="/api", tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse}},
)
def get_health(request: Request) -> HealthResponse:
    """Verify backend availability via a lightweight PostgreSQL ping."""
    if not ping_database(request.app.state.engine):
        raise AppError(
            status_code=503,
            code="SERVICE_UNAVAILABLE",
            message="Service temporarily unavailable.",
            details={"database": "unreachable"},
        )
    return HealthResponse(status="ok", database="ok")


@router.get("/config/public", response_model=PublicConfigResponse)
def get_public_config(
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> PublicConfigResponse:
    """Return non-secret public configuration required by the frontend."""
    return PublicConfigResponse(
        support_email=settings.support_email,
        network_layer_min_zoom=settings.network_layer_min_zoom,
        max_upload_mb=settings.max_upload_mb,
    )
