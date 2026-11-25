"""Domain models, configuration, and errors for hybrid routing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pyproj import Geod

RoutingCostMode = Literal["distance", "hops"]
RoutingAlgorithm = Literal["pgr_dijkstra", "pgr_bdDijkstra"]
SegmentSource = Literal["network", "external"]
SearchTier = Literal["close", "far"]
RouteBranch = Literal[
    "connected",
    "entry_equals_exit",
    "disconnected",
    "no_entry_fallback",
    "entry_no_exit_fallback",
]

DRIVING_ACCESS_PROFILE = "driving-car"
DEFAULT_FALLBACK_PROFILE = "cycling-mountain"

_WGS84_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class Coordinate:
    """WGS84 geographic coordinate matching API `{lat, lng, ele?}` payloads."""

    lat: float
    lng: float
    ele: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.lat) or not (-90.0 <= self.lat <= 90.0):
            raise ValueError(f"Latitude must be finite and in [-90, 90], got {self.lat!r}.")
        if not math.isfinite(self.lng) or not (-180.0 <= self.lng <= 180.0):
            raise ValueError(f"Longitude must be finite and in [-180, 180], got {self.lng!r}.")
        if self.ele is not None and not math.isfinite(self.ele):
            raise ValueError(f"Elevation must be finite when provided, got {self.ele!r}.")

    def to_dict(self) -> dict[str, float]:
        """Serialize coordinate for JSON payloads, omitting `ele` when unavailable."""
        payload: dict[str, float] = {"lat": self.lat, "lng": self.lng}
        if self.ele is not None:
            payload["ele"] = self.ele
        return payload


def geodesic_distance_m(origin: Coordinate, destination: Coordinate) -> float:
    """Return exact WGS84 ellipsoidal distance in meters between two coordinates."""
    if origin.lat == destination.lat and origin.lng == destination.lng:
        return 0.0
    _, _, dist_m = _WGS84_GEOD.inv(origin.lng, origin.lat, destination.lng, destination.lat)
    return float(dist_m)


@dataclass(frozen=True, slots=True)
class NearestNodeMatch:
    """Result of indexed geographic KNN search for a graph entry or exit node."""

    node_id: int
    coord: Coordinate
    distance_m: float
    search_tier: SearchTier


@dataclass(frozen=True, slots=True)
class ExternalRouteResult:
    """Normalized geometry and telemetry returned by an external routing provider."""

    provider: str
    profile: str
    points: tuple[Coordinate, ...]
    distance_m: float | None = None
    duration_s: float | None = None
    latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class RouteSegment:
    """Single ordered section of a hybrid route (internal edge or external connector)."""

    source: SegmentSource
    points: tuple[Coordinate, ...]
    edge_id: int | None = None
    provider_profile: str | None = None
    avg_difficulty: float | None = None
    display_level: int | None = None

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("RouteSegment requires at least 2 ordered points.")
        if self.source == "network":
            if self.edge_id is None:
                raise ValueError("Network RouteSegment must include edge_id.")
            if self.provider_profile is not None:
                raise ValueError("Network RouteSegment must not set provider_profile.")
            if self.display_level is not None and not (1 <= self.display_level <= 3):
                raise ValueError("display_level must be between 1 and 3.")
        else:
            if self.edge_id is not None:
                raise ValueError("External RouteSegment must not set edge_id.")
            if not self.provider_profile:
                raise ValueError("External RouteSegment must set provider_profile.")
            if self.avg_difficulty is not None or self.display_level is not None:
                raise ValueError("External RouteSegment must not carry trail difficulty.")


@dataclass(frozen=True, slots=True)
class NetworkPathResult:
    """Exact pgRouting shortest path across the internal trail network."""

    segments: tuple[RouteSegment, ...]
    total_cost: float
    algorithm: RoutingAlgorithm
    cost_mode: RoutingCostMode
    node_ids: tuple[int, ...]
    edge_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HybridRouteResult:
    """Complete, continuity-verified two-point hybrid route leg."""

    origin: Coordinate
    destination: Coordinate
    branch: RouteBranch
    segments: tuple[RouteSegment, ...]
    entry_node: NearestNodeMatch | None = None
    exit_node: NearestNodeMatch | None = None
    cost_mode: RoutingCostMode = "distance"
    algorithm: RoutingAlgorithm = "pgr_dijkstra"
    graph_revision: int | None = None
    internal_query_ms: float = 0.0
    external_latency_ms: float = 0.0
    continuity_gaps_m: tuple[float, ...] = ()
    endpoint_snap_gaps_m: tuple[float, float] = (0.0, 0.0)

    def flattened_points(self) -> tuple[Coordinate, ...]:
        """Return all route points in order, collapsing identical adjacent junction points."""
        ordered: list[Coordinate] = []
        for segment in self.segments:
            for pt in segment.points:
                if ordered and ordered[-1] == pt:
                    continue
                ordered.append(pt)
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Configuration for hybrid routing leg calculation and provider behavior."""

    close_radius_m: float = 2000.0
    far_radius_m: float = 10000.0
    cost_mode: RoutingCostMode = "distance"
    algorithm: RoutingAlgorithm = "pgr_dijkstra"
    access_profile: str = DRIVING_ACCESS_PROFILE
    fallback_profile: str = DEFAULT_FALLBACK_PROFILE
    continuity_tolerance_m: float = 150.0
    provider_timeout_s: float = 10.0
    provider_max_retries: int = 2

    def __post_init__(self) -> None:
        if not math.isfinite(self.close_radius_m) or self.close_radius_m <= 0:
            raise ValueError("close_radius_m must be finite and > 0.")
        if not math.isfinite(self.far_radius_m) or self.far_radius_m < self.close_radius_m:
            raise ValueError("far_radius_m must be finite and >= close_radius_m.")
        if not math.isfinite(self.continuity_tolerance_m) or self.continuity_tolerance_m < 0:
            raise ValueError("continuity_tolerance_m must be finite and >= 0.")
        if not math.isfinite(self.provider_timeout_s) or self.provider_timeout_s <= 0:
            raise ValueError("provider_timeout_s must be finite and > 0.")
        if self.provider_max_retries < 0:
            raise ValueError("provider_max_retries must be >= 0.")
        if not self.access_profile.strip():
            raise ValueError("access_profile must be non-empty.")
        if not self.fallback_profile.strip():
            raise ValueError("fallback_profile must be non-empty.")


class RoutingError(Exception):
    """Base error for routing calculation failures."""


class RoutingProviderError(RoutingError):
    """Structured error raised when an external routing provider fails."""

    def __init__(
        self,
        code: str,
        status_code: int | None,
        retryable: bool,
        message: str,
        *,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.message = message
        self.provider = provider


class RouteContinuityError(RoutingError):
    """Raised when consecutive sections exceed the allowed continuity tolerance."""

    def __init__(
        self,
        gap_m: float,
        between_sections: str,
        message: str | None = None,
    ) -> None:
        resolved = message or (
            f"Route continuity gap of {gap_m:.2f} m between {between_sections}."
        )
        super().__init__(resolved)
        self.gap_m = gap_m
        self.between_sections = between_sections
        self.message = resolved
