from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response payload for GET /api/health."""

    status: Literal["ok"]
    database: Literal["ok"]


class PublicConfigResponse(BaseModel):
    """Non-secret client configuration exposed via GET /api/config/public."""

    support_email: str
    network_layer_min_zoom: int
    max_upload_mb: int
