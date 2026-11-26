"""Frozen runtime-checkable service and adapter protocols (T-009).

``RoutingProvider`` is re-exported directly from ``app.integrations.routing.protocol``
to guarantee zero drift with T-004 providers. ``GeocodingProvider`` is strictly
separated from ``RoutingProvider`` (FR-025, ARCH §4.20).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from app.contracts.common import (
    BatchImportSource,
    ContributionLevelInfo,
    CreditState,
    DifficultyLevel,
    EntitlementDenialReason,
    PreferredLanguage,
    UploadStatus,
)
from app.domain.geo.models import IngestionResult, ParsedTrack, SnappingConfig
from app.integrations.routing.protocol import RoutingProvider


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Server-side route entitlement evaluation result for a user/session."""

    allowed: bool
    reason: EntitlementDenialReason | None
    admin_exempt: bool
    contribution_eligible: bool
    qualifying_count: int
    min_required_tracks: int
    active_count: int
    free_allowance_limit: int
    free_allowance_remaining: int
    grace_until: datetime | None
    grace_valid: bool
    contribution_level: ContributionLevelInfo


@dataclass(frozen=True, slots=True)
class AllowanceReservationResult:
    """Outcome of Tx1 entitlement check and optional free-allowance reservation."""

    outcome: Literal[
        "reserved",
        "replayed",
        "non_allowance",
        "denied",
        "in_flight",
        "expired",
        "conflict",
    ]
    attempt_id: UUID | None = None
    expires_at: datetime | None = None
    replayed_response: Mapping[str, Any] | None = None
    denial_reason: EntitlementDenialReason | None = None


@dataclass(frozen=True, slots=True)
class AllowanceSettlementResult:
    """Outcome of Tx2 atomic ownership-verified reservation settlement."""

    outcome: Literal[
        "consumed",
        "released_independent_eligibility",
        "session_invalid",
        "reservation_expired",
    ]
    consumed: bool
    replay_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GraceApplicationResult:
    """Outcome of applying bounded session grace inside a rejection transaction."""

    grace_applied: bool
    sessions_updated: int
    grace_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ValidatedUploadDraft:
    """Server-owned validated upload draft persisted after durable storage write."""

    upload_id: UUID
    filename: str
    original_ref: str
    parsed_track: ParsedTrack
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmedUploadResult:
    """Durable acceptance outcome returned immediately (202) on upload confirm."""

    upload_id: UUID
    status: UploadStatus
    credit_state: CreditState
    overall_difficulty: DifficultyLevel
    accepted_at: datetime
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class EdgeRatingSummary:
    """Committed edge rating aggregate and caller's own rating (zero other-user PII)."""

    edge_id: int
    avg_difficulty: float
    display_level: DifficultyLevel
    report_count: int
    user_rating: DifficultyLevel | None
    graph_revision: int


@dataclass(frozen=True, slots=True)
class TrackSourceEntry:
    """Descriptor for one candidate file discovered by a batch TrackSource."""

    source_ref: str
    filename: str
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class StoredFileRef:
    """Reference returned after durable file publication (fsync+rename or S3 PUT)."""

    key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredFileInfo:
    """Metadata returned when listing keys for aged orphan cleanup (AC-92)."""

    key: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class GeocodingMatch:
    """Normalized location search candidate returned by a GeocodingProvider."""

    provider_place_id: str
    display_name: str
    lat: float
    lng: float
    category: str | None = None
    address: str | None = None
    bbox: tuple[float, float, float, float] | None = None


@runtime_checkable
class EligibilityService(Protocol):
    """Protocol for route entitlement evaluation, grace, and free-allowance accounting."""

    def evaluate(
        self,
        user_id: int,
        *,
        session_id: UUID | None = None,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        """Evaluate current route-creation eligibility and contribution level."""
        ...

    def reserve_free_allowance(
        self,
        user_id: int,
        request_id: UUID,
        coord_fingerprint: str,
        *,
        ttl_seconds: int,
        session_id: UUID | None = None,
    ) -> AllowanceReservationResult:
        """Execute Tx1 replay/eligibility check and owned reservation claim if needed."""
        ...

    def settle_free_allowance(
        self,
        user_id: int,
        request_id: UUID,
        attempt_id: UUID,
        response_payload: Mapping[str, Any],
        *,
        session_id: UUID,
        replay_window_seconds: int,
    ) -> AllowanceSettlementResult:
        """Execute Tx2 in-flight reevaluation and ownership-verified settlement."""
        ...

    def release_free_allowance(
        self,
        user_id: int,
        request_id: UUID,
        attempt_id: UUID,
    ) -> bool:
        """Release an owned RESERVED allowance slot after route calculation failure."""
        ...

    def apply_rejection_grace(
        self,
        user_id: int,
        *,
        rejected_upload_id: UUID,
        now: datetime | None = None,
    ) -> GraceApplicationResult:
        """Grant bounded grace to currently eligible active sessions on business rejection."""
        ...


@runtime_checkable
class IngestionService(Protocol):
    """Protocol for upload validation, durable confirmation, and async graph ingestion."""

    def validate_upload(
        self,
        user_id: int,
        filename: str,
        content: bytes,
    ) -> ValidatedUploadDraft:
        """Validate untrusted track bytes, publish durably to storage, and create a DRAFT."""
        ...

    def confirm_upload(
        self,
        user_id: int,
        upload_id: UUID,
        overall_difficulty: DifficultyLevel,
        *,
        idempotency_key: str,
    ) -> ConfirmedUploadResult:
        """Atomically lock DRAFT, check duplicates/idempotency, set PENDING, and enqueue."""
        ...

    def ingest_upload(
        self,
        upload_id: UUID,
        attempt_id: UUID,
        *,
        config_snapshot: SnappingConfig,
    ) -> IngestionResult:
        """Execute fenced asynchronous ingestion for a confirmed user upload."""
        ...

    def ingest_batch_file(
        self,
        batch_file_id: int,
        attempt_id: UUID,
        parsed_track: ParsedTrack,
        *,
        config_snapshot: SnappingConfig,
    ) -> IngestionResult:
        """Execute fenced asynchronous ingestion for a single admin batch-import file."""
        ...


@runtime_checkable
class RatingService(Protocol):
    """Protocol for independent concurrent edge difficulty ratings (FR-021, AC-84/85)."""

    def get_edge_rating(
        self,
        edge_id: int,
        *,
        user_id: int | None = None,
    ) -> EdgeRatingSummary:
        """Return effective difficulty, report count, and optional caller rating for an edge."""
        ...

    def upsert_edge_rating(
        self,
        edge_id: int,
        user_id: int,
        difficulty: DifficultyLevel,
    ) -> EdgeRatingSummary:
        """Atomically lock edge, upsert caller's report, recompute mean, and bump revision."""
        ...


@runtime_checkable
class TrackSource(Protocol):
    """Protocol for listing and reading batch-import track files (FR-013, ARCH §4.15)."""

    source_kind: BatchImportSource

    def list_tracks(self) -> Sequence[TrackSourceEntry]:
        """List candidate track entries available from this batch source."""
        ...

    def open_track_bytes(self, source_ref: str) -> bytes:
        """Read raw bytes for a discovered track entry."""
        ...


@runtime_checkable
class FileStorageAdapter(Protocol):
    """Protocol for durable upload file publication and safe orphan cleanup (AC-91/92)."""

    def put_durable(self, key: str, data: bytes) -> StoredFileRef:
        """Durably persist bytes (temp+fsync+rename or verified S3 PUT) before DB insert."""
        ...

    def read_bytes(self, key: str) -> bytes:
        """Read previously persisted bytes by storage key."""
        ...

    def delete_if_exists(self, key: str) -> bool:
        """Delete a stored object if present, returning True when removed."""
        ...

    def list_keys(self, *, prefix: str = "") -> Sequence[StoredFileInfo]:
        """List stored file descriptors matching prefix for aged orphan sweeping."""
        ...


@runtime_checkable
class GeocodingProvider(Protocol):
    """Provider-independent map location search protocol (FR-025, AC-104..107).

    Strictly separated from ``RoutingProvider`` so geocoding and routing backends
    can be evaluated and swapped independently.
    """

    name: str

    def search(
        self,
        query: str,
        *,
        lang: PreferredLanguage | None = None,
        limit: int = 5,
    ) -> Sequence[GeocodingMatch]:
        """Search Hebrew or English locations and return normalized geographic candidates."""
        ...


__all__ = [
    "AllowanceReservationResult",
    "AllowanceSettlementResult",
    "ConfirmedUploadResult",
    "EdgeRatingSummary",
    "EligibilityDecision",
    "EligibilityService",
    "FileStorageAdapter",
    "GeocodingMatch",
    "GeocodingProvider",
    "GraceApplicationResult",
    "IngestionService",
    "RatingService",
    "RoutingProvider",
    "StoredFileInfo",
    "StoredFileRef",
    "TrackSource",
    "TrackSourceEntry",
    "ValidatedUploadDraft",
]
