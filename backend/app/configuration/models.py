"""Application runtime configuration ORM model and canonical FR-022 defaults."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

CONFIG_KEY_MIN_REQUIRED_TRACKS: str = "MIN_REQUIRED_TRACKS"
CONFIG_KEY_FREE_ROUTE_CREATIONS: str = "FREE_ROUTE_CREATIONS"
CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS: str = "REJECTED_TRACK_GRACE_PERIOD_HOURS"
CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M: str = "POINT_SIMPLIFICATION_DISTANCE_M"
CONFIG_KEY_NODE_SNAP_RADIUS_M: str = "NODE_SNAP_RADIUS_M"

CANONICAL_APP_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        CONFIG_KEY_MIN_REQUIRED_TRACKS,
        CONFIG_KEY_FREE_ROUTE_CREATIONS,
        CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS,
        CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M,
        CONFIG_KEY_NODE_SNAP_RADIUS_M,
    }
)

# Confirmed Gate 1 (PRD §10.1) & T-007 synthetic POC-locked defaults for FR-022 settings.
DEFAULT_APP_CONFIG: Mapping[str, int | float] = MappingProxyType(
    {
        CONFIG_KEY_MIN_REQUIRED_TRACKS: 1,
        CONFIG_KEY_FREE_ROUTE_CREATIONS: 0,
        CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS: 24,
        CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M: 20.0,
        CONFIG_KEY_NODE_SNAP_RADIUS_M: 20.0,
    }
)


class AppConfig(Base):
    """Runtime FR-022 configuration key/value row stored in ``app_config``."""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = [
    "AppConfig",
    "CANONICAL_APP_CONFIG_KEYS",
    "CONFIG_KEY_FREE_ROUTE_CREATIONS",
    "CONFIG_KEY_MIN_REQUIRED_TRACKS",
    "CONFIG_KEY_NODE_SNAP_RADIUS_M",
    "CONFIG_KEY_POINT_SIMPLIFICATION_DISTANCE_M",
    "CONFIG_KEY_REJECTED_TRACK_GRACE_PERIOD_HOURS",
    "DEFAULT_APP_CONFIG",
]
