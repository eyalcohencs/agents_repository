"""Synchronous RoutingProvider adapter protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.integrations.routing.models import Coordinate, ExternalRouteResult


@runtime_checkable
class RoutingProvider(Protocol):
    """Protocol implemented by external routing provider adapters."""

    name: str

    def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str,
    ) -> ExternalRouteResult:
        """Calculate an external route between origin and destination for the given profile."""
        ...
