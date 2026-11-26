"""Frozen API primitives, machine-readable codes, and pure domain helpers (T-009)."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ErrorResponse
from app.integrations.routing.models import Coordinate

PreferredLanguage = Literal["he", "en"]
DEFAULT_PREFERRED_LANGUAGE: PreferredLanguage = "he"

DifficultyLevel = Literal[1, 2, 3]
BatchImportSource = Literal["cloud", "server"]
ContributionLevelLabel = Literal["Explorer", "Adventurer", "Trailblazer", "Trail Master"]
IngestionRunSourceType = Literal["upload", "batch_file"]


class ErrorCode(StrEnum):
    """Stable machine-readable API error codes localized by the frontend (FR-027)."""

    # Standard HTTP envelope defaults (app.core.errors)
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # Authentication & session lifecycle
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USERNAME_TAKEN = "USERNAME_TAKEN"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_REVOKED = "SESSION_REVOKED"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    CSRF_VALIDATION_FAILED = "CSRF_VALIDATION_FAILED"

    # Entitlement, reservation & replay safety
    ROUTE_NOT_ALLOWED = "ROUTE_NOT_ALLOWED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    REQUEST_ID_EXPIRED = "REQUEST_ID_EXPIRED"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
    REQUEST_IN_FLIGHT = "REQUEST_IN_FLIGHT"
    IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"

    # Upload validation, storage & ingestion
    EMPTY_FILE = "EMPTY_FILE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    MALFORMED_XML = "MALFORMED_XML"
    UNSAFE_XML = "UNSAFE_XML"
    INVALID_COORDINATE = "INVALID_COORDINATE"
    DEGENERATE_GEOMETRY = "DEGENERATE_GEOMETRY"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_POINTS = "TOO_MANY_POINTS"
    DRAFT_LIMIT_EXCEEDED = "DRAFT_LIMIT_EXCEEDED"
    DUPLICATE_UPLOAD = "DUPLICATE_UPLOAD"
    STORAGE_FAILURE = "STORAGE_FAILURE"

    # Routing, geocoding, viewport & batch imports
    ROUTING_PROVIDER_ERROR = "ROUTING_PROVIDER_ERROR"
    ROUTING_PROVIDER_QUOTA = "ROUTING_PROVIDER_QUOTA"
    ROUTE_CONTINUITY_FAILED = "ROUTE_CONTINUITY_FAILED"
    GEOCODING_PROVIDER_ERROR = "GEOCODING_PROVIDER_ERROR"
    GEOCODING_PROVIDER_QUOTA = "GEOCODING_PROVIDER_QUOTA"
    VIEWPORT_TOO_LARGE = "VIEWPORT_TOO_LARGE"
    BATCH_IMPORT_ALREADY_RUNNING = "BATCH_IMPORT_ALREADY_RUNNING"


class EntitlementDenialReason(StrEnum):
    """Machine-readable reasons attached to ROUTE_NOT_ALLOWED denials (ARCH §8)."""

    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    ALLOWANCE_EXHAUSTED = "ALLOWANCE_EXHAUSTED"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"


class UploadStatus(StrEnum):
    """Durable user track submission lifecycle states (PRD §6.5, ARCH §7)."""

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRY = "RETRY"
    TECHNICAL_FAILED = "TECHNICAL_FAILED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


CREDIT_BEARING_UPLOAD_STATUSES: frozenset[UploadStatus] = frozenset(
    {
        UploadStatus.PENDING,
        UploadStatus.PROCESSING,
        UploadStatus.RETRY,
        UploadStatus.TECHNICAL_FAILED,
        UploadStatus.ACTIVE,
    }
)


class CreditState(StrEnum):
    """Contribution credit state exposed on My Tracks and upload confirmations."""

    NONE = "NONE"
    PROVISIONAL = "PROVISIONAL"
    PERMANENT = "PERMANENT"


class RejectionCode(StrEnum):
    """Deterministic business rejection codes for accepted uploads (ARCH §4.8)."""

    DEGENERATE_GEOMETRY = "DEGENERATE_GEOMETRY"
    OUTSIDE_SERVICE_AREA = "OUTSIDE_SERVICE_AREA"
    QUALITY_RULE_FAILED = "QUALITY_RULE_FAILED"


class UserRole(StrEnum):
    """Server-enforced user account roles (PRD §2, Gate 1 confirmed)."""

    REGULAR = "regular"
    SYSTEM = "system"
    INACTIVE = "inactive"


class BatchImportStatus(StrEnum):
    """Lifecycle states for administrator batch import jobs (ARCH §7)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class BatchFileStatus(StrEnum):
    """Per-file durable tracking states inside a batch import (ARCH §7)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRY = "RETRY"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class IngestionRunStatus(StrEnum):
    """Derived lifecycle states for per-submission ingestion provenance runs."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ReservationState(StrEnum):
    """Owned free-allowance reservation states (ARCH §4.10, §7)."""

    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    NON_ALLOWANCE = "NON_ALLOWANCE"


class ContributionLevelKey(StrEnum):
    """Stable machine-readable community contribution level keys (FR-024, FR-027)."""

    EXPLORER = "explorer"
    ADVENTURER = "adventurer"
    TRAILBLAZER = "trailblazer"
    TRAIL_MASTER = "trail_master"


class ContributionLevelInfo(BaseModel):
    """Derived community contribution level summary (ACTIVE uploads only, ARCH §4.19)."""

    model_config = ConfigDict(extra="forbid")

    level: ContributionLevelLabel
    badge_key: ContributionLevelKey
    active_count: int = Field(ge=0)
    next_level: ContributionLevelLabel | None = None
    tracks_to_next: int | None = Field(default=None, ge=1)


def derive_contribution_level(active_count: int) -> ContributionLevelInfo:
    """Derive a user's contribution level strictly from their ACTIVE upload count.

    Boundaries (ARCH §4.19, SPEC AC-100/AC-101):
    - 0..5   -> Explorer     (6 - active_count to Adventurer)
    - 6..9   -> Adventurer   (10 - active_count to Trailblazer)
    - 10..19 -> Trailblazer  (20 - active_count to Trail Master)
    - 20+    -> Trail Master (next_level=None, tracks_to_next=None)
    """
    if isinstance(active_count, bool) or active_count < 0:
        raise ValueError(f"active_count must be a non-negative integer, got {active_count!r}.")

    if active_count <= 5:
        return ContributionLevelInfo(
            level="Explorer",
            badge_key=ContributionLevelKey.EXPLORER,
            active_count=active_count,
            next_level="Adventurer",
            tracks_to_next=6 - active_count,
        )
    if active_count <= 9:
        return ContributionLevelInfo(
            level="Adventurer",
            badge_key=ContributionLevelKey.ADVENTURER,
            active_count=active_count,
            next_level="Trailblazer",
            tracks_to_next=10 - active_count,
        )
    if active_count <= 19:
        return ContributionLevelInfo(
            level="Trailblazer",
            badge_key=ContributionLevelKey.TRAILBLAZER,
            active_count=active_count,
            next_level="Trail Master",
            tracks_to_next=20 - active_count,
        )
    return ContributionLevelInfo(
        level="Trail Master",
        badge_key=ContributionLevelKey.TRAIL_MASTER,
        active_count=active_count,
        next_level=None,
        tracks_to_next=None,
    )


class CoordinateSchema(BaseModel):
    """WGS84 API coordinate `{lat, lng, ele?}` with finite bounds matching Coordinate."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0, allow_inf_nan=False)
    lng: float = Field(ge=-180.0, le=180.0, allow_inf_nan=False)
    ele: float | None = Field(default=None, allow_inf_nan=False)

    @field_validator("lat", "lng", "ele", mode="before")
    @classmethod
    def _reject_bool_and_non_finite(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Coordinate values must be numeric, not boolean.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Coordinate values must be finite.")
        return value

    def to_coordinate(self) -> Coordinate:
        """Convert schema payload to the immutable routing domain Coordinate."""
        return Coordinate(lat=self.lat, lng=self.lng, ele=self.ele)

    @classmethod
    def from_coordinate(cls, coord: Coordinate) -> CoordinateSchema:
        """Build a CoordinateSchema from a routing domain Coordinate."""
        return cls(lat=coord.lat, lng=coord.lng, ele=coord.ele)


__all__ = [
    "BatchFileStatus",
    "BatchImportSource",
    "BatchImportStatus",
    "CREDIT_BEARING_UPLOAD_STATUSES",
    "ContributionLevelInfo",
    "ContributionLevelKey",
    "ContributionLevelLabel",
    "CoordinateSchema",
    "CreditState",
    "DEFAULT_PREFERRED_LANGUAGE",
    "DifficultyLevel",
    "EntitlementDenialReason",
    "ErrorCode",
    "ErrorResponse",
    "IngestionRunSourceType",
    "IngestionRunStatus",
    "PreferredLanguage",
    "RejectionCode",
    "ReservationState",
    "UploadStatus",
    "UserRole",
    "derive_contribution_level",
]
