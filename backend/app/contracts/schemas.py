"""Frozen Pydantic v2 API request/response schemas covering ARCH §8 endpoints (T-009).

Health and public config response models are re-exported directly from
``app.health.schemas`` to guarantee zero drift. Mutation request models enforce
``ConfigDict(extra="forbid")`` so unexpected or injected fields (such as client-supplied
replacement coordinates on ``UploadConfirmRequest`` per AC-55) fail validation.

Security hardening (QA_REPORT.md §4):
- Auth string inputs reject NUL (``\\x00``) and ASCII control chars (``\\x00-\\x1f``, ``\\x7f``).
- ``AdminConfigUpdateRequest`` enforces finite upper bounds preventing extreme/overflow configs.
- ``RouteCreateRequest.include_diagnostics`` documents mandatory server-side ``system``-role RBAC.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.common import (
    DEFAULT_PREFERRED_LANGUAGE,
    BatchImportSource,
    BatchImportStatus,
    ContributionLevelInfo,
    CoordinateSchema,
    CreditState,
    DifficultyLevel,
    EntitlementDenialReason,
    IngestionRunSourceType,
    IngestionRunStatus,
    PreferredLanguage,
    RejectionCode,
    UploadStatus,
    UserRole,
)
from app.health.schemas import HealthResponse, PublicConfigResponse
from app.integrations.routing.models import RouteBranch, RoutingAlgorithm, RoutingCostMode

_USERNAME_PATTERN = re.compile(r"^[A-Za-z]{4,32}$")
_PASSWORD_ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9@$!%*?&]{4,16}$")
_PASSWORD_SPECIAL_CHARS = frozenset("@$!%*?&")

MAX_MIN_REQUIRED_TRACKS = 10_000
MAX_FREE_ROUTE_CREATIONS = 10_000
MAX_POINT_SIMPLIFICATION_DISTANCE_M = 10_000.0
MAX_NODE_SNAP_RADIUS_M = 10_000.0
MAX_REJECTED_TRACK_GRACE_PERIOD_HOURS = 8760.0


def _reject_control_chars(value: str) -> str:
    """Reject NUL (0x00) and ASCII control characters (0x00-0x1f, 0x7f) in auth strings."""
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in value):
        raise ValueError("Control characters are not permitted.")
    return value


def _reject_bool_numeric(value: object) -> object:
    """Reject Python bools (subclasses of int) and non-finite floats before coercion."""
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid numeric inputs.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Value must be finite.")
    return value


def _validate_difficulty_input(value: object) -> object:
    """Ensure difficulty inputs are strict integers in {1, 2, 3} (never bool/float)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Difficulty must be an integer between 1 and 3.")
    if value not in (1, 2, 3):
        raise ValueError("Difficulty must be 1, 2, or 3.")
    return value


def _validate_email_format(value: str) -> str:
    """Validate basic email structure matching SPEC §2.1 without optional extras."""
    _reject_control_chars(value)
    cleaned = value.strip()
    if len(cleaned) < 3 or len(cleaned) > 50 or any(ch.isspace() for ch in cleaned):
        raise ValueError("Email must be a valid address up to 50 characters.")
    local, sep, domain = cleaned.partition("@")
    if (
        not sep
        or not local
        or not domain
        or "@" in domain
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        raise ValueError("Email must be a valid email address.")
    return cleaned


def _validate_non_empty_name(value: str) -> str:
    """Validate FR-001 first/last name rules (1..16 chars, non-empty after trim)."""
    _reject_control_chars(value)
    trimmed = value.strip()
    if not (1 <= len(trimmed) <= 16):
        raise ValueError("Name must be between 1 and 16 non-whitespace characters.")
    if len(value) > 16:
        raise ValueError("Name must not exceed 16 characters.")
    return trimmed


# ---------------------------------------------------------------------------
# Auth & User Preferences (FR-001..FR-004, FR-027, ARCH §8)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Payload for POST /api/auth/register (FR-001, AC-1..AC-6, AC-111)."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=16)
    last_name: str = Field(min_length=1, max_length=16)
    email: str = Field(min_length=3, max_length=50)
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=4, max_length=16)
    preferred_language: PreferredLanguage = DEFAULT_PREFERRED_LANGUAGE

    @field_validator("first_name", "last_name")
    @classmethod
    def _check_names(cls, value: str) -> str:
        return _validate_non_empty_name(value)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_format(value)

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        _reject_control_chars(value)
        if not _USERNAME_PATTERN.fullmatch(value):
            raise ValueError("Username must be 4 to 32 English letters (A-Z, a-z).")
        return value

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        _reject_control_chars(value)
        if not _PASSWORD_ALLOWED_PATTERN.fullmatch(value):
            raise ValueError(
                "Password must be 4 to 16 characters using only A-Z, a-z, 0-9, and @$!%*?&."
            )
        has_lower = any("a" <= ch <= "z" for ch in value)
        has_upper = any("A" <= ch <= "Z" for ch in value)
        has_digit = any("0" <= ch <= "9" for ch in value)
        has_special = any(ch in _PASSWORD_SPECIAL_CHARS for ch in value)
        if not (has_lower and has_upper and has_digit and has_special):
            raise ValueError(
                "Password must include lowercase, uppercase, digit, and one of @$!%*?&."
            )
        return value


class RegisterResponse(BaseModel):
    """Response for POST /api/auth/register (AC-3, AC-5)."""

    status: Literal["succeed"] = "succeed"
    user_id: int
    username: str
    user_type: Literal[UserRole.REGULAR] = UserRole.REGULAR
    preferred_language: PreferredLanguage = DEFAULT_PREFERRED_LANGUAGE


class LoginRequest(BaseModel):
    """Payload for POST /api/auth/login (FR-002, AC-7..AC-8)."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        _reject_control_chars(value)
        if not value.strip():
            raise ValueError("Username must not be blank.")
        return value

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return _reject_control_chars(value)


class AuthenticatedUserSchema(BaseModel):
    """Authenticated user profile returned by login/session endpoints (zero secrets)."""

    id: int
    username: str
    first_name: str
    last_name: str
    email: str
    user_type: UserRole
    preferred_language: PreferredLanguage = DEFAULT_PREFERRED_LANGUAGE


class LoginResponse(BaseModel):
    """Response for POST /api/auth/login including non-secret session_id (ARCH §4.6, §8)."""

    user: AuthenticatedUserSchema
    session_id: UUID
    preferred_language: PreferredLanguage = DEFAULT_PREFERRED_LANGUAGE
    contribution_level: ContributionLevelInfo


class LogoutRequest(BaseModel):
    """Optional payload for POST /api/auth/logout multi-tab safety (ARCH §4.6, AC-11)."""

    model_config = ConfigDict(extra="forbid")

    expected_session_id: UUID | None = None


class LogoutResponse(BaseModel):
    """Response for POST /api/auth/logout indicating revocation or supersession."""

    revoked: bool
    reason: Literal["revoked", "superseded", "already_revoked"] = "revoked"


class AuthMeResponse(BaseModel):
    """Response for GET /api/auth/me (ARCH §8, AC-14, AC-100..AC-103, AC-111)."""

    id: int
    username: str
    first_name: str
    last_name: str
    email: str
    user_type: UserRole
    preferred_language: PreferredLanguage = DEFAULT_PREFERRED_LANGUAGE
    session_id: UUID
    contribution_level: ContributionLevelInfo


class UserPreferencesPatchRequest(BaseModel):
    """Payload for PATCH /api/me/preferences (FR-027, AC-109..AC-111)."""

    model_config = ConfigDict(extra="forbid")

    preferred_language: PreferredLanguage


class UserPreferencesResponse(BaseModel):
    """Response for PATCH /api/me/preferences."""

    preferred_language: PreferredLanguage


# ---------------------------------------------------------------------------
# Routing & Map Location Search (FR-007, FR-025, ARCH §4.7, §4.18, §4.20, §8)
# ---------------------------------------------------------------------------


class RouteCreateRequest(BaseModel):
    """Payload for POST /api/routes (FR-007, ARCH §4.7/§4.10, AC-22..AC-31, AC-82..AC-83)."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    start_lat: float = Field(ge=-90.0, le=90.0, allow_inf_nan=False)
    start_lng: float = Field(ge=-180.0, le=180.0, allow_inf_nan=False)
    end_lat: float = Field(ge=-90.0, le=90.0, allow_inf_nan=False)
    end_lng: float = Field(ge=-180.0, le=180.0, allow_inf_nan=False)
    include_diagnostics: bool = Field(
        default=False,
        description=(
            "Request admin playground diagnostics. Server MUST enforce `system`-role RBAC "
            "before honoring `include_diagnostics=True`; non-system callers must never "
            "receive diagnostics."
        ),
    )

    @field_validator("start_lat", "start_lng", "end_lat", "end_lng", mode="before")
    @classmethod
    def _check_coords(cls, value: object) -> object:
        return _reject_bool_numeric(value)


class RouteSegmentSchema(BaseModel):
    """Single ordered section of a hybrid route response (ARCH §4.7, AC-24..AC-30)."""

    source: Literal["network", "external"]
    points: list[CoordinateSchema] = Field(min_length=2)
    edge_id: int | None = None
    provider_profile: str | None = None
    avg_difficulty: float | None = Field(default=None, ge=1.0, le=3.0, allow_inf_nan=False)
    display_level: DifficultyLevel | None = None

    @model_validator(mode="after")
    def _validate_source_fields(self) -> RouteSegmentSchema:
        if self.source == "network":
            if self.edge_id is None:
                raise ValueError("Network route segment must include edge_id.")
            if self.provider_profile is not None:
                raise ValueError("Network route segment must not include provider_profile.")
        else:
            if self.edge_id is not None:
                raise ValueError("External route segment must not include edge_id.")
            if not self.provider_profile:
                raise ValueError("External route segment must include provider_profile.")
            if self.avg_difficulty is not None or self.display_level is not None:
                raise ValueError("External route segment must not include trail difficulty.")
        return self


class RouteDiagnosticsSchema(BaseModel):
    """Admin-only playground diagnostics attached to POST /api/routes (ARCH §4.18, AC-38)."""

    branch: RouteBranch
    algorithm: RoutingAlgorithm
    cost_mode: RoutingCostMode
    entry_node_id: int | None = None
    exit_node_id: int | None = None
    entry_distance_m: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    exit_distance_m: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    graph_revision: int | None = Field(default=None, ge=0)
    internal_query_ms: float = Field(ge=0.0, allow_inf_nan=False)
    external_latency_ms: float = Field(ge=0.0, allow_inf_nan=False)


class RouteCreateResponse(BaseModel):
    """Response for POST /api/routes (zero metrics per AC-31, zero author PII per AC-93)."""

    request_id: UUID
    segments: list[RouteSegmentSchema] = Field(min_length=1)
    replayed: bool = False
    diagnostics: RouteDiagnosticsSchema | None = None


class LocationSearchResultSchema(BaseModel):
    """Single geographic location match for GET /api/map/search (FR-025)."""

    place_id: str
    display_name: str
    lat: float = Field(ge=-90.0, le=90.0, allow_inf_nan=False)
    lng: float = Field(ge=-180.0, le=180.0, allow_inf_nan=False)
    category: str | None = None
    address: str | None = None


class LocationSearchResponse(BaseModel):
    """Response for GET /api/map/search?q=...&lang=... (AC-104..AC-107)."""

    query: str
    results: list[LocationSearchResultSchema]


# ---------------------------------------------------------------------------
# Uploads, Ingestion Provenance & Entitlement (FR-018..FR-021, FR-024, ARCH §8)
# ---------------------------------------------------------------------------


class UploadPreviewSegmentSchema(BaseModel):
    """Server-owned preview polyline segment returned for a validated DRAFT upload."""

    segment_index: int = Field(ge=0)
    points: list[CoordinateSchema] = Field(min_length=2)


class UploadValidateResponse(BaseModel):
    """Response for POST /api/uploads/validate (AC-53..AC-55, zero credit)."""

    upload_id: UUID
    filename: str
    status: Literal[UploadStatus.DRAFT] = UploadStatus.DRAFT
    credit_state: Literal[CreditState.NONE] = CreditState.NONE
    point_count: int = Field(ge=2)
    segments: list[UploadPreviewSegmentSchema] = Field(min_length=1)
    expires_at: datetime


class UploadConfirmRequest(BaseModel):
    """Payload for POST /api/uploads/{id}/confirm (AC-55/56: rejects extra/injected fields)."""

    model_config = ConfigDict(extra="forbid")

    overall_difficulty: DifficultyLevel

    @field_validator("overall_difficulty", mode="before")
    @classmethod
    def _check_difficulty(cls, value: object) -> object:
        return _validate_difficulty_input(value)


class EntitlementResponse(BaseModel):
    """Response for GET /api/me/entitlement (ARCH §8, AC-66..AC-72, AC-100..AC-103)."""

    allowed: bool
    reason: EntitlementDenialReason | None = None
    admin_exempt: bool = False
    contribution_eligible: bool
    qualifying_count: int = Field(ge=0)
    min_required_tracks: int = Field(ge=1)
    free_allowance_limit: int = Field(ge=0)
    free_allowance_remaining: int = Field(ge=0)
    grace_until: datetime | None = None
    contribution_level: ContributionLevelInfo


class UploadConfirmResponse(BaseModel):
    """Response (202 Accepted) for POST /api/uploads/{id}/confirm (AC-58..AC-60)."""

    upload_id: UUID
    filename: str
    status: UploadStatus
    credit_state: CreditState
    overall_difficulty: DifficultyLevel
    accepted_at: datetime
    entitlement: EntitlementResponse


class UploadDeleteResponse(BaseModel):
    """Response for DELETE /api/uploads/{id} draft cancellation (AC-55, AC-92)."""

    upload_id: UUID
    deleted: Literal[True] = True


class IngestionRunSummarySchema(BaseModel):
    """Per-submission ingestion provenance summary (ARCH §7, T-008)."""

    id: UUID
    source_type: IngestionRunSourceType
    status: IngestionRunStatus
    track_id: str
    graph_revision_before: int = Field(ge=0)
    created_at: datetime
    completed_at: datetime | None = None


class MyUploadItemSchema(BaseModel):
    """Single non-draft upload row on My Tracks (FR-019, AC-73, zero raw stack traces)."""

    upload_id: UUID
    filename: str
    status: UploadStatus
    credit_state: CreditState
    overall_difficulty: DifficultyLevel | None = None
    rejection_code: RejectionCode | None = None
    safe_error_message: str | None = None
    submitted_at: datetime
    processed_at: datetime | None = None
    latest_ingestion_run: IngestionRunSummarySchema | None = None


class MyUploadsResponse(BaseModel):
    """Response for GET /api/me/uploads (FR-019, FR-024, AC-73, AC-100..AC-102)."""

    uploads: list[MyUploadItemSchema]
    entitlement: EntitlementResponse
    contribution_level: ContributionLevelInfo


# ---------------------------------------------------------------------------
# Network Viewport & Edge Ratings (FR-021, FR-023, ARCH §4.8, §4.12, §8)
# ---------------------------------------------------------------------------


class NetworkEdgeSchema(BaseModel):
    """Active selectable network edge (zero uploader or rating-author PII per AC-93)."""

    edge_id: int
    points: list[CoordinateSchema] = Field(min_length=2)
    length_m: float = Field(gt=0.0, allow_inf_nan=False)
    avg_difficulty: float = Field(ge=1.0, le=3.0, allow_inf_nan=False)
    display_level: DifficultyLevel
    report_count: int = Field(ge=0)


class NetworkEdgesViewportResponse(BaseModel):
    """Response for GET /api/network/edges?bbox=...&zoom=... (AC-74..AC-76, AC-87, AC-93)."""

    edges: list[NetworkEdgeSchema]
    truncated: bool
    graph_revision: int = Field(ge=0)


class EdgeRatingPutRequest(BaseModel):
    """Payload for PUT /api/network/edges/{id}/rating (AC-74..AC-76, AC-84)."""

    model_config = ConfigDict(extra="forbid")

    difficulty: DifficultyLevel

    @field_validator("difficulty", mode="before")
    @classmethod
    def _check_difficulty(cls, value: object) -> object:
        return _validate_difficulty_input(value)


class EdgeRatingResponse(BaseModel):
    """Response for GET/PUT /api/network/edges/{id}/rating (zero other-user PII, AC-93)."""

    edge_id: int
    avg_difficulty: float = Field(ge=1.0, le=3.0, allow_inf_nan=False)
    display_level: DifficultyLevel
    report_count: int = Field(ge=0)
    user_rating: DifficultyLevel | None = None
    graph_revision: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Administration Endpoints (FR-010..FR-013, FR-022, ARCH §4.13, §8)
# ---------------------------------------------------------------------------


class AdminUserSchema(BaseModel):
    """Read-only user row for GET /api/admin/users (AC-42: no password_hash or created_at)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    first_name: str
    last_name: str
    email: str
    user_type: UserRole


class AdminUsersResponse(BaseModel):
    """Response for GET /api/admin/users (AC-42)."""

    users: list[AdminUserSchema]


class AdminConfigResponse(BaseModel):
    """Response for GET/PUT /api/admin/config (FR-022, AC-46..AC-47, AC-77, zero secrets)."""

    min_required_tracks: int = Field(ge=1)
    point_simplification_distance_m: float = Field(gt=0.0, allow_inf_nan=False)
    node_snap_radius_m: float = Field(gt=0.0, allow_inf_nan=False)
    free_route_creations: int = Field(ge=0)
    rejected_track_grace_period_hours: float = Field(ge=0.0, allow_inf_nan=False)
    updated_at: datetime | None = None
    updated_by: int | None = None


class AdminConfigUpdateRequest(BaseModel):
    """Atomic update payload for PUT /api/admin/config (FR-022, AC-46..AC-47, AC-77)."""

    model_config = ConfigDict(extra="forbid")

    min_required_tracks: int = Field(ge=1, le=MAX_MIN_REQUIRED_TRACKS)
    point_simplification_distance_m: float = Field(
        gt=0.0,
        le=MAX_POINT_SIMPLIFICATION_DISTANCE_M,
        allow_inf_nan=False,
    )
    node_snap_radius_m: float = Field(
        gt=0.0,
        le=MAX_NODE_SNAP_RADIUS_M,
        allow_inf_nan=False,
    )
    free_route_creations: int = Field(ge=0, le=MAX_FREE_ROUTE_CREATIONS)
    rejected_track_grace_period_hours: float = Field(
        ge=0.0,
        le=MAX_REJECTED_TRACK_GRACE_PERIOD_HOURS,
        allow_inf_nan=False,
    )

    @field_validator(
        "min_required_tracks",
        "point_simplification_distance_m",
        "node_snap_radius_m",
        "free_route_creations",
        "rejected_track_grace_period_hours",
        mode="before",
    )
    @classmethod
    def _check_numeric(cls, value: object) -> object:
        return _reject_bool_numeric(value)


class BatchImportStartRequest(BaseModel):
    """Payload for POST /api/admin/imports (FR-013, AC-43..AC-45, AC-89..AC-90)."""

    model_config = ConfigDict(extra="forbid")

    source: BatchImportSource


class BatchImportStatusResponse(BaseModel):
    """Response (202 Accepted) and status model for POST /api/admin/imports."""

    batch_id: UUID
    source: BatchImportSource
    status: BatchImportStatus
    total_files: int = Field(ge=0)
    imported_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime | None = None


class GeoJsonLineStringGeometry(BaseModel):
    """RFC 7946 GeoJSON LineString geometry (`[lng, lat]` coordinate order)."""

    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(min_length=2)

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(
        cls, coords: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        for lng, lat in coords:
            if not (math.isfinite(lng) and -180.0 <= lng <= 180.0):
                raise ValueError(f"GeoJSON longitude out of bounds: {lng!r}.")
            if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
                raise ValueError(f"GeoJSON latitude out of bounds: {lat!r}.")
        return coords


class GeoJsonPointGeometry(BaseModel):
    """RFC 7946 GeoJSON Point geometry (`[lng, lat]` or `[lng, lat, ele]`)."""

    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float] | tuple[float, float, float]

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinate(
        cls, coord: tuple[float, float] | tuple[float, float, float]
    ) -> tuple[float, float] | tuple[float, float, float]:
        lng, lat = coord[0], coord[1]
        if not (math.isfinite(lng) and -180.0 <= lng <= 180.0):
            raise ValueError(f"GeoJSON longitude out of bounds: {lng!r}.")
        if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
            raise ValueError(f"GeoJSON latitude out of bounds: {lat!r}.")
        if len(coord) == 3 and not math.isfinite(coord[2]):
            raise ValueError(f"GeoJSON elevation must be finite: {coord[2]!r}.")
        return coord


class AdminNetworkEdgeProperties(BaseModel):
    """Edge properties for GET /api/admin/network/edges (zero author PII per AC-93)."""

    edge_id: int
    length_m: float = Field(gt=0.0, allow_inf_nan=False)
    avg_difficulty: float = Field(ge=1.0, le=3.0, allow_inf_nan=False)
    display_level: DifficultyLevel
    report_count: int = Field(ge=0)


class AdminNetworkNodeProperties(BaseModel):
    """Node properties for GET /api/admin/network/nodes (zero author PII per AC-93)."""

    node_id: int
    ele: float | None = Field(default=None, allow_inf_nan=False)


class AdminNetworkEdgeFeature(BaseModel):
    """GeoJSON Feature for one ACTIVE trail network edge."""

    type: Literal["Feature"] = "Feature"
    id: int
    geometry: GeoJsonLineStringGeometry
    properties: AdminNetworkEdgeProperties


class AdminNetworkNodeFeature(BaseModel):
    """GeoJSON Feature for one ACTIVE trail network node."""

    type: Literal["Feature"] = "Feature"
    id: int
    geometry: GeoJsonPointGeometry
    properties: AdminNetworkNodeProperties


class AdminNetworkEdgesGeoJsonResponse(BaseModel):
    """Typed GeoJSON FeatureCollection for GET /api/admin/network/edges (AC-39..41, AC-86)."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    graph_revision: int = Field(ge=0)
    features: list[AdminNetworkEdgeFeature]


class AdminNetworkNodesGeoJsonResponse(BaseModel):
    """Typed GeoJSON FeatureCollection for GET /api/admin/network/nodes (AC-39..41, AC-86)."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    graph_revision: int = Field(ge=0)
    features: list[AdminNetworkNodeFeature]


class AdminNetworkGeoJsonResponse(BaseModel):
    """Polymorphic GeoJSON FeatureCollection union retained for backwards compatibility."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    graph_revision: int = Field(ge=0)
    features: list[AdminNetworkEdgeFeature | AdminNetworkNodeFeature]


__all__ = [
    "AdminConfigResponse",
    "AdminConfigUpdateRequest",
    "AdminNetworkEdgeFeature",
    "AdminNetworkEdgeProperties",
    "AdminNetworkEdgesGeoJsonResponse",
    "AdminNetworkGeoJsonResponse",
    "AdminNetworkNodeFeature",
    "AdminNetworkNodeProperties",
    "AdminNetworkNodesGeoJsonResponse",
    "AdminUserSchema",
    "AdminUsersResponse",
    "AuthMeResponse",
    "AuthenticatedUserSchema",
    "BatchImportStartRequest",
    "BatchImportStatusResponse",
    "EdgeRatingPutRequest",
    "EdgeRatingResponse",
    "EntitlementResponse",
    "GeoJsonLineStringGeometry",
    "GeoJsonPointGeometry",
    "HealthResponse",
    "IngestionRunSummarySchema",
    "LocationSearchResponse",
    "LocationSearchResultSchema",
    "LoginRequest",
    "LoginResponse",
    "LogoutRequest",
    "LogoutResponse",
    "MAX_FREE_ROUTE_CREATIONS",
    "MAX_MIN_REQUIRED_TRACKS",
    "MAX_NODE_SNAP_RADIUS_M",
    "MAX_POINT_SIMPLIFICATION_DISTANCE_M",
    "MAX_REJECTED_TRACK_GRACE_PERIOD_HOURS",
    "MyUploadItemSchema",
    "MyUploadsResponse",
    "NetworkEdgeSchema",
    "NetworkEdgesViewportResponse",
    "PublicConfigResponse",
    "RegisterRequest",
    "RegisterResponse",
    "RouteCreateRequest",
    "RouteCreateResponse",
    "RouteDiagnosticsSchema",
    "RouteSegmentSchema",
    "UploadConfirmRequest",
    "UploadConfirmResponse",
    "UploadDeleteResponse",
    "UploadPreviewSegmentSchema",
    "UploadValidateResponse",
    "UserPreferencesPatchRequest",
    "UserPreferencesResponse",
]
