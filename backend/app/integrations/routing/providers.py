"""External routing provider adapters (OpenRouteService, GraphHopper, Fake)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal

import httpx

from app.integrations.routing.models import (
    Coordinate,
    ExternalRouteResult,
    RoutingProviderError,
    geodesic_distance_m,
)

logger = logging.getLogger(__name__)

ORS_DEFAULT_BASE_URL = "https://api.openrouteservice.org"
GRAPHHOPPER_DEFAULT_BASE_URL = "https://graphhopper.com/api/1"

GRAPHHOPPER_PROFILE_MAP: dict[str, str] = {
    "driving-car": "car",
    "cycling-mountain": "mtb",
    "foot-hiking": "hike",
    "foot-walking": "foot",
}

FaultKind = Literal["429", "503", "timeout", "empty_geometry", "continuity_gap"]


def map_graphhopper_profile(profile: str) -> str:
    """Map canonical OnTrack/ORS profile names onto GraphHopper profile names."""
    normalized = profile.strip()
    if not normalized:
        raise ValueError("Routing profile must be non-empty.")
    return GRAPHHOPPER_PROFILE_MAP.get(normalized, normalized)


def _parse_geojson_coordinates(
    raw_coords: Any,
    *,
    provider: str,
    status_code: int,
) -> tuple[Coordinate, ...]:
    """Parse GeoJSON `[lng, lat(, ele)]` coordinate arrays into `Coordinate` tuples."""
    if not isinstance(raw_coords, list) or len(raw_coords) < 2:
        raise RoutingProviderError(
            code="EMPTY_GEOMETRY",
            status_code=status_code,
            retryable=False,
            message=f"{provider} returned empty or degenerate route geometry.",
            provider=provider,
        )

    points: list[Coordinate] = []
    for item in raw_coords:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise RoutingProviderError(
                code="INVALID_RESPONSE",
                status_code=status_code,
                retryable=False,
                message=f"{provider} returned malformed coordinate entry.",
                provider=provider,
            )
        try:
            lng = float(item[0])
            lat = float(item[1])
            ele = float(item[2]) if len(item) >= 3 and item[2] is not None else None
            points.append(Coordinate(lat=lat, lng=lng, ele=ele))
        except (TypeError, ValueError) as exc:
            raise RoutingProviderError(
                code="INVALID_RESPONSE",
                status_code=status_code,
                retryable=False,
                message=f"{provider} returned out-of-range or non-numeric coordinates.",
                provider=provider,
            ) from exc

    return tuple(points)


def _classify_http_error(provider: str, status_code: int) -> RoutingProviderError:
    """Map an HTTP error status code to a structured, secret-free provider error."""
    if status_code == 429:
        return RoutingProviderError(
            code="RATE_LIMITED",
            status_code=429,
            retryable=False,
            message=f"{provider} rate limit or quota exceeded (HTTP 429).",
            provider=provider,
        )
    if status_code in (401, 403):
        return RoutingProviderError(
            code="AUTH_ERROR",
            status_code=status_code,
            retryable=False,
            message=f"{provider} rejected authentication credentials (HTTP {status_code}).",
            provider=provider,
        )
    if 500 <= status_code < 600:
        return RoutingProviderError(
            code="PROVIDER_UNAVAILABLE",
            status_code=status_code,
            retryable=True,
            message=f"{provider} returned transient server error (HTTP {status_code}).",
            provider=provider,
        )
    return RoutingProviderError(
        code="PROVIDER_CLIENT_ERROR",
        status_code=status_code,
        retryable=False,
        message=f"{provider} rejected routing request (HTTP {status_code}).",
        provider=provider,
    )


class _BaseHttpRoutingProvider:
    """Shared bounded-timeout, limited-retry HTTP scaffolding without secret logging."""

    name: str

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        cleaned_key = api_key.strip()
        if not cleaned_key:
            raise ValueError(f"{self.name} requires a non-empty API key.")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0.")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0.")
        if retry_backoff_s < 0:
            raise ValueError("retry_backoff_s must be >= 0.")

        self._api_key = cleaned_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._sleep_fn = sleep_fn
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(timeout=httpx.Timeout(timeout_s))
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"timeout_s={self._timeout_s!r}, max_retries={self._max_retries!r}, "
            "api_key='<redacted>')"
        )

    def close(self) -> None:
        """Close the underlying HTTP client when owned by this provider instance."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> _BaseHttpRoutingProvider:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _send_with_retries(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        profile_label: str,
    ) -> tuple[httpx.Response, float]:
        """Execute an HTTP request retrying ONLY on transient 5xx and transport/timeout errors."""
        total_attempts = 1 + self._max_retries
        last_error: RoutingProviderError | None = None

        for attempt in range(total_attempts):
            started = time.perf_counter()
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._timeout_s,
                )
            except httpx.TimeoutException:
                last_error = RoutingProviderError(
                    code="PROVIDER_TIMEOUT",
                    status_code=None,
                    retryable=True,
                    message=f"{self.name} request timed out after {self._timeout_s:.1f}s.",
                    provider=self.name,
                )
                logger.warning(
                    "Routing provider timeout",
                    extra={
                        "provider": self.name,
                        "profile": profile_label,
                        "attempt": attempt + 1,
                        "total_attempts": total_attempts,
                    },
                )
            except httpx.TransportError:
                last_error = RoutingProviderError(
                    code="PROVIDER_NETWORK_ERROR",
                    status_code=None,
                    retryable=True,
                    message=f"{self.name} transport/network error while fetching route.",
                    provider=self.name,
                )
                logger.warning(
                    "Routing provider network error",
                    extra={
                        "provider": self.name,
                        "profile": profile_label,
                        "attempt": attempt + 1,
                        "total_attempts": total_attempts,
                    },
                )
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if 200 <= response.status_code < 300:
                    return response, elapsed_ms

                error = _classify_http_error(self.name, response.status_code)
                if not error.retryable:
                    logger.warning(
                        "Routing provider non-retryable HTTP error",
                        extra={
                            "provider": self.name,
                            "profile": profile_label,
                            "status_code": response.status_code,
                            "error_code": error.code,
                        },
                    )
                    raise error

                last_error = error
                logger.warning(
                    "Routing provider transient HTTP error",
                    extra={
                        "provider": self.name,
                        "profile": profile_label,
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "total_attempts": total_attempts,
                    },
                )

            if attempt + 1 < total_attempts and self._retry_backoff_s > 0:
                self._sleep_fn(self._retry_backoff_s * (2**attempt))

        assert last_error is not None
        raise last_error


class OpenRouteServiceProvider(_BaseHttpRoutingProvider):
    """OpenRouteService Directions GeoJSON adapter (`POST /v2/directions/{profile}/geojson`)."""

    name = "openrouteservice"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ORS_DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout_s=timeout_s,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            client=client,
            sleep_fn=sleep_fn,
        )

    def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str,
    ) -> ExternalRouteResult:
        cleaned_profile = profile.strip()
        if not cleaned_profile:
            raise ValueError("Routing profile must be non-empty.")

        url = f"{self._base_url}/v2/directions/{cleaned_profile}/geojson"
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/geo+json, application/json",
        }
        payload = {
            "coordinates": [
                [origin.lng, origin.lat],
                [destination.lng, destination.lat],
            ],
            "elevation": True,
        }

        response, latency_ms = self._send_with_retries(
            method="POST",
            url=url,
            headers=headers,
            json_body=payload,
            profile_label=cleaned_profile,
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingProviderError(
                code="INVALID_RESPONSE",
                status_code=response.status_code,
                retryable=False,
                message="openrouteservice returned non-JSON response payload.",
                provider=self.name,
            ) from exc

        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list) or not features:
            raise RoutingProviderError(
                code="EMPTY_GEOMETRY",
                status_code=response.status_code,
                retryable=False,
                message="openrouteservice response contained no route features.",
                provider=self.name,
            )

        feature0 = features[0]
        geometry = feature0.get("geometry") if isinstance(feature0, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            raise RoutingProviderError(
                code="EMPTY_GEOMETRY",
                status_code=response.status_code,
                retryable=False,
                message="openrouteservice response did not contain a LineString geometry.",
                provider=self.name,
            )

        points = _parse_geojson_coordinates(
            geometry.get("coordinates"),
            provider=self.name,
            status_code=response.status_code,
        )

        distance_m: float | None = None
        duration_s: float | None = None
        properties = feature0.get("properties") if isinstance(feature0, dict) else None
        summary = properties.get("summary") if isinstance(properties, dict) else None
        if isinstance(summary, dict):
            raw_dist = summary.get("distance")
            raw_dur = summary.get("duration")
            if isinstance(raw_dist, (int, float)):
                distance_m = float(raw_dist)
            if isinstance(raw_dur, (int, float)):
                duration_s = float(raw_dur)

        return ExternalRouteResult(
            provider=self.name,
            profile=cleaned_profile,
            points=points,
            distance_m=distance_m,
            duration_s=duration_s,
            latency_ms=latency_ms,
        )


class GraphHopperProvider(_BaseHttpRoutingProvider):
    """GraphHopper Routing API adapter (`POST /route`, mapping ORS profiles to GH profiles)."""

    name = "graphhopper"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GRAPHHOPPER_DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout_s=timeout_s,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            client=client,
            sleep_fn=sleep_fn,
        )

    def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str,
    ) -> ExternalRouteResult:
        mapped_profile = map_graphhopper_profile(profile)
        url = f"{self._base_url}/route"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "points": [
                [origin.lng, origin.lat],
                [destination.lng, destination.lat],
            ],
            "profile": mapped_profile,
            "points_encoded": False,
            "elevation": True,
            "instructions": False,
        }

        response, latency_ms = self._send_with_retries(
            method="POST",
            url=url,
            headers=headers,
            params={"key": self._api_key},
            json_body=payload,
            profile_label=mapped_profile,
        )

        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingProviderError(
                code="INVALID_RESPONSE",
                status_code=response.status_code,
                retryable=False,
                message="graphhopper returned non-JSON response payload.",
                provider=self.name,
            ) from exc

        paths = data.get("paths") if isinstance(data, dict) else None
        if not isinstance(paths, list) or not paths:
            raise RoutingProviderError(
                code="EMPTY_GEOMETRY",
                status_code=response.status_code,
                retryable=False,
                message="graphhopper response contained no route paths.",
                provider=self.name,
            )

        path0 = paths[0]
        raw_points_obj = path0.get("points") if isinstance(path0, dict) else None
        raw_coords = (
            raw_points_obj.get("coordinates") if isinstance(raw_points_obj, dict) else None
        )
        points = _parse_geojson_coordinates(
            raw_coords,
            provider=self.name,
            status_code=response.status_code,
        )

        distance_m: float | None = None
        duration_s: float | None = None
        if isinstance(path0, dict):
            raw_dist = path0.get("distance")
            raw_time_ms = path0.get("time")
            if isinstance(raw_dist, (int, float)):
                distance_m = float(raw_dist)
            if isinstance(raw_time_ms, (int, float)):
                duration_s = float(raw_time_ms) / 1000.0

        return ExternalRouteResult(
            provider=self.name,
            profile=mapped_profile,
            points=points,
            distance_m=distance_m,
            duration_s=duration_s,
            latency_ms=latency_ms,
        )


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """Recorded invocation against `FakeRoutingProvider`."""

    origin: Coordinate
    destination: Coordinate
    profile: str


class FakeRoutingProvider:
    """Deterministic fake provider with scripted responses and structured fault injection."""

    def __init__(
        self,
        *,
        name: str = "fake",
        scripted_responses: Sequence[ExternalRouteResult | Exception | FaultKind] | None = None,
        fault: FaultKind | None = None,
        fail_on_call: int | None = None,
        continuity_gap_offset_deg: float = 0.01,
        simulated_latency_ms: float = 1.0,
    ) -> None:
        self.name = name
        self._scripted = list(scripted_responses) if scripted_responses is not None else []
        self._fault = fault
        self._fail_on_call = fail_on_call
        self._continuity_gap_offset_deg = continuity_gap_offset_deg
        self._simulated_latency_ms = simulated_latency_ms
        self.calls: list[ProviderCall] = []

    def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str,
    ) -> ExternalRouteResult:
        call_index = len(self.calls) + 1
        self.calls.append(ProviderCall(origin=origin, destination=destination, profile=profile))

        if self._scripted:
            action = self._scripted.pop(0)
            if isinstance(action, Exception):
                raise action
            if isinstance(action, ExternalRouteResult):
                return action
            return self._execute_fault(action, origin, destination, profile)

        if self._fault is not None and (
            self._fail_on_call is None or self._fail_on_call == call_index
        ):
            return self._execute_fault(self._fault, origin, destination, profile)

        return self._build_continuous_result(origin, destination, profile)

    def _execute_fault(
        self,
        fault: FaultKind,
        origin: Coordinate,
        destination: Coordinate,
        profile: str,
    ) -> ExternalRouteResult:
        if fault == "429":
            raise RoutingProviderError(
                code="RATE_LIMITED",
                status_code=429,
                retryable=False,
                message=f"{self.name} simulated rate limit exceeded (HTTP 429).",
                provider=self.name,
            )
        if fault == "503":
            raise RoutingProviderError(
                code="PROVIDER_UNAVAILABLE",
                status_code=503,
                retryable=True,
                message=f"{self.name} simulated temporary outage (HTTP 503).",
                provider=self.name,
            )
        if fault == "timeout":
            raise RoutingProviderError(
                code="PROVIDER_TIMEOUT",
                status_code=None,
                retryable=True,
                message=f"{self.name} simulated request timeout.",
                provider=self.name,
            )
        if fault == "empty_geometry":
            raise RoutingProviderError(
                code="EMPTY_GEOMETRY",
                status_code=200,
                retryable=False,
                message=f"{self.name} simulated empty route geometry.",
                provider=self.name,
            )
        if fault == "continuity_gap":
            shifted_end = Coordinate(
                lat=min(90.0, destination.lat + self._continuity_gap_offset_deg),
                lng=destination.lng,
                ele=destination.ele,
            )
            return ExternalRouteResult(
                provider=self.name,
                profile=profile,
                points=(origin, shifted_end),
                distance_m=geodesic_distance_m(origin, shifted_end),
                duration_s=60.0,
                latency_ms=self._simulated_latency_ms,
            )
        raise ValueError(f"Unsupported fault kind: {fault!r}")

    def _build_continuous_result(
        self,
        origin: Coordinate,
        destination: Coordinate,
        profile: str,
    ) -> ExternalRouteResult:
        midpoint = Coordinate(
            lat=(origin.lat + destination.lat) / 2.0,
            lng=(origin.lng + destination.lng) / 2.0,
            ele=(
                (origin.ele + destination.ele) / 2.0
                if origin.ele is not None and destination.ele is not None
                else None
            ),
        )
        points = (
            (origin, destination)
            if origin.lat == destination.lat and origin.lng == destination.lng
            else (origin, midpoint, destination)
        )
        dist = geodesic_distance_m(origin, destination)
        return ExternalRouteResult(
            provider=self.name,
            profile=profile,
            points=points,
            distance_m=dist,
            duration_s=dist / 10.0 if dist > 0 else 0.0,
            latency_ms=self._simulated_latency_ms,
        )
