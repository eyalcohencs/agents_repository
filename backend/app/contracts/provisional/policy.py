"""Provisional policy registry tracking Gate 1 decisions and Gate 2 open items.

All contracts under ``app.contracts.provisional`` remain explicitly provisional
ahead of the T-009 contract freeze. Gate 1 product decisions confirmed in T-000
(``docs/PRD.md`` §10.1) are reflected as provisional defaults here, while Gate 2
POC- and real-data-dependent items remain explicitly open pending T-007 and T-028.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

CONTRACT_STAGE: Literal["provisional"] = "provisional"
CONTRACT_FREEZE_TASK: Literal["T-009"] = "T-009"


class PolicyStatus(StrEnum):
    """Lifecycle status of a policy-governed contract field or decision."""

    PROVISIONAL = "provisional"
    GATE_1_CONFIRMED = "gate_1_confirmed"
    GATE_2_OPEN = "gate_2_open"
    GATE_3_OPEN = "gate_3_open"


@dataclass(frozen=True, slots=True)
class PolicyAnnotation:
    """Metadata tying a contract field or rule back to its PRD/Architecture gate."""

    key: str
    topic: str
    gate: Literal["gate_1", "gate_2", "gate_3"]
    status: PolicyStatus
    current_default: str
    affected_contracts: tuple[str, ...]
    source_ref: str
    notes: str


_GATE_1_ENTRIES: tuple[PolicyAnnotation, ...] = (
    PolicyAnnotation(
        key="free_route_creations",
        topic="Free route allowance (FREE_ROUTE_CREATIONS)",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="0 (configurable >= 0 via FR-022)",
        affected_contracts=(
            "AdminConfigResponse.free_route_creations",
            "AdminConfigUpdateRequest.free_route_creations",
            "EntitlementResponse.free_allowance_limit",
            "EntitlementResponse.free_allowance_remaining",
            "EligibilityService.reserve_free_allowance",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes=(
            "Confirmed contribution-only initial default (0); reservation/replay "
            "contracts remain active whenever an admin configures a value > 0."
        ),
    ),
    PolicyAnnotation(
        key="rejection_grace_period_hours",
        topic="Rejection grace (REJECTED_TRACK_GRACE_PERIOD_HOURS) & config-edit rule",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default=(
            "24 hours on current eligible session; edits apply to future rejections only"
        ),
        affected_contracts=(
            "AdminConfigResponse.rejected_track_grace_period_hours",
            "AdminConfigUpdateRequest.rejected_track_grace_period_hours",
            "EntitlementResponse.grace_until",
            "EligibilityService.apply_rejection_grace",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Grace is bound to active sessions at rejection time and never extended.",
    ),
    PolicyAnnotation(
        key="in_flight_entitlement_changes",
        topic="In-flight entitlement and session changes during route calculation",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default=(
            "Release reservation unconsumed on independent eligibility; "
            "reject 401 if session revoked or expired"
        ),
        affected_contracts=(
            "EligibilityService.settle_free_allowance",
            "AllowanceSettlementResult.outcome",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Evaluated inside atomic settlement transaction before returning 200.",
    ),
    PolicyAnnotation(
        key="admin_routing_exemption",
        topic="System administrator exemption from upload threshold",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="system admins exempt for route creation and playground testing",
        affected_contracts=(
            "EntitlementResponse.admin_exempt",
            "EligibilityDecision.admin_exempt",
            "RouteCreateResponse.diagnostics",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="System admins never consume free route allowance.",
    ),
    PolicyAnnotation(
        key="same_user_duplicate_fingerprint",
        topic="Same-user duplicate upload detection policy",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Canonical 6-decimal normalized_fingerprint (ignoring XML metadata)",
        affected_contracts=(
            "IngestionService.validate_upload",
            "IngestionService.confirm_upload",
            "ErrorCode.DUPLICATE_UPLOAD",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Applies across all credit-bearing upload statuses; file_sha256 kept for integrity.",
    ),
    PolicyAnnotation(
        key="cross_user_duplicates",
        topic="Cross-user duplicate uploads",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Allowed",
        affected_contracts=(
            "IngestionService.confirm_upload",
            "IngestionService.ingest_upload",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Separated ingestion_runs preserve per-submission provenance without PK collision.",
    ),
    PolicyAnnotation(
        key="zero_new_edge_credit",
        topic="Credit for valid uploads adding zero new edges",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Qualifies, subject to same-user duplicate policy",
        affected_contracts=(
            "IngestionService.ingest_upload",
            "UploadStatus.ACTIVE",
            "CreditState.PERMANENT",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Reusing 100% existing edges still transitions to ACTIVE.",
    ),
    PolicyAnnotation(
        key="one_editable_rating_per_user_edge",
        topic="One editable explicit rating per user per edge",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default=(
            "One editable report per (edge_id, user_id); re-rating replaces prior report"
        ),
        affected_contracts=(
            "RatingService.upsert_edge_rating",
            "EdgeRatingPutRequest",
            "EdgeRatingResponse",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Enforced atomically under per-edge row lock and unique (edge_id, user_id).",
    ),
    PolicyAnnotation(
        key="initial_difficulty_excluded_from_mean",
        topic="Initial edge difficulty exclusion from explicit rating mean",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Excluded from mean; used as fallback only when report_count == 0",
        affected_contracts=(
            "RatingService.get_edge_rating",
            "EdgeRatingResponse.avg_difficulty",
            "NetworkEdgeSchema.avg_difficulty",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Subsequent uploads never modify initial_difficulty or explicit ratings.",
    ),
    PolicyAnnotation(
        key="rating_without_route_entitlement",
        topic="Explicit edge rating authorization and upload credit impact",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default=(
            "Allowed for active authenticated users without route entitlement; 0 credit"
        ),
        affected_contracts=(
            "RatingService.upsert_edge_rating",
            "EdgeRatingPutRequest",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Active session required; guests receive 401 and inactive users are denied.",
    ),
    PolicyAnnotation(
        key="password_policy",
        topic="Registration password validation policy",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Legacy FR-001 4-16 char rule ([A-Za-z0-9@$!%*?&] with all 4 classes)",
        affected_contracts=("RegisterRequest.password",),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Modern passphrase policy deferred; Argon2id hashing required regardless.",
    ),
    PolicyAnnotation(
        key="supported_upload_formats",
        topic="Supported user upload file formats",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="GPX only for user uploads (all <trk>/<trkseg> preserved separately)",
        affected_contracts=(
            "IngestionService.validate_upload",
            "UploadValidateResponse",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Admin batch imports continue to support GPX and KML (skipping TWL).",
    ),
    PolicyAnnotation(
        key="inactive_user_access",
        topic="Inactive account login and protected API policy",
        gate="gate_1",
        status=PolicyStatus.GATE_1_CONFIRMED,
        current_default="Block login (generic error) and deny all protected endpoints/screens",
        affected_contracts=(
            "UserRole.INACTIVE",
            "EntitlementDenialReason.ACCOUNT_INACTIVE",
            "ErrorCode.ACCOUNT_INACTIVE",
        ),
        source_ref="docs/PRD.md §10.1 (T-000 Gate 1)",
        notes="Login returns identical generic invalid-credentials message to prevent enumeration.",
    ),
)

_GATE_2_ENTRIES: tuple[PolicyAnnotation, ...] = (
    PolicyAnnotation(
        key="routing_cost_mode",
        topic="Routing cost objective & entry/exit candidate selection",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default="distance (meters) recommended; hops comparison mode; strict nearest-node",
        affected_contracts=(
            "RouteDiagnosticsSchema.cost_mode",
            "RouteDiagnosticsSchema.algorithm",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & docs/ARCHITECTURE.md §11",
        notes="Exact pgr_dijkstra locked in T-007 synthetic gate; objective finalized in T-028.",
    ),
    PolicyAnnotation(
        key="point_simplification_distance_m",
        topic="Point reduction threshold (POINT_SIMPLIFICATION_DISTANCE_M)",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default="20.0 m (synthetic POC baseline)",
        affected_contracts=(
            "AdminConfigResponse.point_simplification_distance_m",
            "AdminConfigUpdateRequest.point_simplification_distance_m",
            "IngestionService.ingest_upload",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & T-003/T-007/T-028",
        notes="Real-dataset calibration and topology error limits pending T-028.",
    ),
    PolicyAnnotation(
        key="node_snap_radius_m",
        topic="Node snapping radius (NODE_SNAP_RADIUS_M)",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default="20.0 m (synthetic POC baseline; 20/10 m nominated for T-028 real archive)",
        affected_contracts=(
            "AdminConfigResponse.node_snap_radius_m",
            "AdminConfigUpdateRequest.node_snap_radius_m",
            "IngestionService.ingest_upload",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & T-003/T-007/T-028",
        notes="Independent positive finite setting; real-dataset validation pending T-028.",
    ),
    PolicyAnnotation(
        key="routing_provider",
        topic="External routing provider selection (ORS vs GraphHopper) & topology limits",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default=(
            "RoutingProvider protocol abstraction (live ORS vs GraphHopper pending T-028)"
        ),
        affected_contracts=(
            "RoutingProvider",
            "RouteSegmentSchema.provider_profile",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & T-004/T-007/T-028",
        notes="Live Israeli pair comparison requires human-supplied API keys in T-028.",
    ),
    PolicyAnnotation(
        key="geocoding_provider",
        topic="Map location search provider (GeocodingProvider, FR-025)",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default=(
            "Provider-independent GeocodingProvider protocol "
            "(FakeGeocodingProvider in tests)"
        ),
        affected_contracts=(
            "GeocodingProvider",
            "GeocodingMatch",
            "LocationSearchResponse",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & T-030/T-028",
        notes="Provider selected after Israel Hebrew/English coverage, quota, and terms POC.",
    ),
    PolicyAnnotation(
        key="contribution_level_labels",
        topic="Contribution level display names & badge iconography (FR-024)",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default="Explorer (0-5), Adventurer (6-9), Trailblazer (10-19), Trail Master (20+)",
        affected_contracts=(
            "ContributionLevelKey",
            "ContributionLevelInfo",
            "derive_contribution_level",
        ),
        source_ref="docs/PRD.md §10.1 (Gate 2) & docs/ARCHITECTURE.md §4.19",
        notes="Stable machine-readable badge_key values insulate i18n copy changes.",
    ),
    PolicyAnnotation(
        key="business_rejection_codes",
        topic="Enabled post-acceptance business rejection reasons",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default=(
            "Only DEGENERATE_GEOMETRY enabled; "
            "OUTSIDE_SERVICE_AREA & QUALITY_RULE_FAILED disabled"
        ),
        affected_contracts=(
            "RejectionCode",
            "MyUploadItemSchema.rejection_code",
        ),
        source_ref="docs/ARCHITECTURE.md §4.8 & §11",
        notes="Additional rejection rules remain disabled pending product approval.",
    ),
    PolicyAnnotation(
        key="routing_fallback_profile",
        topic="External routing fallback profile & neutral connector treatment",
        gate="gate_2",
        status=PolicyStatus.GATE_2_OPEN,
        current_default="cycling-mountain with null difficulty and unverified-passability legend",
        affected_contracts=("RouteSegmentSchema.provider_profile",),
        source_ref="docs/ARCHITECTURE.md §4.7 & §11",
        notes="Configurable profile token; never represented as proof of 4x4 passability.",
    ),
)

GATE_1_POLICY_ANNOTATIONS: Mapping[str, PolicyAnnotation] = MappingProxyType(
    {entry.key: entry for entry in _GATE_1_ENTRIES}
)

GATE_2_OPEN_FIELDS: Mapping[str, PolicyAnnotation] = MappingProxyType(
    {entry.key: entry for entry in _GATE_2_ENTRIES}
)


def is_provisional_contract() -> bool:
    """Return True while contracts remain provisional ahead of the T-009 freeze."""
    return CONTRACT_STAGE == "provisional"


__all__ = [
    "CONTRACT_FREEZE_TASK",
    "CONTRACT_STAGE",
    "GATE_1_POLICY_ANNOTATIONS",
    "GATE_2_OPEN_FIELDS",
    "PolicyAnnotation",
    "PolicyStatus",
    "is_provisional_contract",
]
