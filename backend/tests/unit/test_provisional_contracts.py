"""Unit tests for T-006 provisional service protocols, policy registry, and draft API schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.contracts import __all__ as contracts_root_all
from app.contracts import provisional
from app.contracts.provisional import (
    CONTRACT_FREEZE_TASK,
    CONTRACT_STAGE,
    CREDIT_BEARING_UPLOAD_STATUSES,
    DEFAULT_PREFERRED_LANGUAGE,
    GATE_1_POLICY_ANNOTATIONS,
    GATE_2_OPEN_FIELDS,
    AdminConfigResponse,
    AdminConfigUpdateRequest,
    AdminNetworkEdgeFeature,
    AdminNetworkEdgeProperties,
    AdminNetworkGeoJsonResponse,
    AdminNetworkNodeFeature,
    AdminNetworkNodeProperties,
    AdminUserSchema,
    AdminUsersResponse,
    AllowanceReservationResult,
    AllowanceSettlementResult,
    AuthMeResponse,
    BatchImportStartRequest,
    BatchImportStatus,
    BatchImportStatusResponse,
    ConfirmedUploadResult,
    ContributionLevelInfo,
    ContributionLevelKey,
    CoordinateSchema,
    CreditState,
    DifficultyLevel,
    EdgeRatingPutRequest,
    EdgeRatingResponse,
    EdgeRatingSummary,
    EligibilityDecision,
    EligibilityService,
    EntitlementDenialReason,
    EntitlementResponse,
    ErrorCode,
    ErrorResponse,
    FileStorageAdapter,
    GeocodingMatch,
    GeocodingProvider,
    GeoJsonLineStringGeometry,
    GeoJsonPointGeometry,
    GraceApplicationResult,
    HealthResponse,
    IngestionService,
    LocationSearchResponse,
    LocationSearchResultSchema,
    LoginRequest,
    LogoutRequest,
    MyUploadItemSchema,
    MyUploadsResponse,
    NetworkEdgeSchema,
    NetworkEdgesViewportResponse,
    PolicyAnnotation,
    PolicyStatus,
    PreferredLanguage,
    PublicConfigResponse,
    RatingService,
    RegisterRequest,
    RejectionCode,
    RouteCreateRequest,
    RouteCreateResponse,
    RouteDiagnosticsSchema,
    RouteSegmentSchema,
    RoutingProvider,
    StoredFileInfo,
    StoredFileRef,
    TrackSource,
    TrackSourceEntry,
    UploadConfirmRequest,
    UploadConfirmResponse,
    UploadPreviewSegmentSchema,
    UploadStatus,
    UploadValidateResponse,
    UserPreferencesPatchRequest,
    UserRole,
    ValidatedUploadDraft,
    derive_contribution_level,
    is_provisional_contract,
)
from app.core.errors import ErrorResponse as CoreErrorResponse
from app.domain.geo.models import (
    IngestionResult,
    ParsedTrack,
    SnappedEdgeRef,
    SnappingConfig,
    TrackPoint,
    TrackSegment,
)
from app.health.schemas import HealthResponse as CoreHealthResponse
from app.health.schemas import PublicConfigResponse as CorePublicConfigResponse
from app.integrations.routing.models import Coordinate
from app.integrations.routing.protocol import RoutingProvider as CoreRoutingProvider
from app.integrations.routing.providers import (
    FakeRoutingProvider,
    GraphHopperProvider,
    OpenRouteServiceProvider,
)

# ---------------------------------------------------------------------------
# Dummy concrete implementations for @runtime_checkable protocol conformance
# ---------------------------------------------------------------------------


class _DummyEligibilityService:
    def evaluate(
        self,
        user_id: int,
        *,
        session_id: UUID | None = None,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        return EligibilityDecision(
            allowed=True,
            reason=None,
            admin_exempt=False,
            contribution_eligible=True,
            qualifying_count=1,
            min_required_tracks=1,
            active_count=1,
            free_allowance_limit=0,
            free_allowance_remaining=0,
            grace_until=None,
            grace_valid=False,
            contribution_level=derive_contribution_level(1),
        )

    def reserve_free_allowance(
        self,
        user_id: int,
        request_id: UUID,
        coord_fingerprint: str,
        *,
        ttl_seconds: int,
        session_id: UUID | None = None,
    ) -> AllowanceReservationResult:
        return AllowanceReservationResult(outcome="non_allowance")

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
        return AllowanceSettlementResult(
            outcome="released_independent_eligibility",
            consumed=False,
        )

    def release_free_allowance(
        self,
        user_id: int,
        request_id: UUID,
        attempt_id: UUID,
    ) -> bool:
        return True

    def apply_rejection_grace(
        self,
        user_id: int,
        *,
        rejected_upload_id: UUID,
        now: datetime | None = None,
    ) -> GraceApplicationResult:
        return GraceApplicationResult(grace_applied=False, sessions_updated=0)


class _DummyIngestionService:
    def validate_upload(
        self,
        user_id: int,
        filename: str,
        content: bytes,
    ) -> ValidatedUploadDraft:
        track = ParsedTrack(
            source_format="gpx",
            segments=(
                TrackSegment(
                    index=0,
                    points=(
                        TrackPoint(lat=32.794, lon=34.989),
                        TrackPoint(lat=32.795, lon=34.990),
                    ),
                ),
            ),
            file_sha256="a" * 64,
            normalized_fingerprint="b" * 64,
            raw_point_count=2,
        )
        return ValidatedUploadDraft(
            upload_id=uuid4(),
            filename=filename,
            original_ref="uploads/sample.gpx",
            parsed_track=track,
            expires_at=datetime.now(UTC),
        )

    def confirm_upload(
        self,
        user_id: int,
        upload_id: UUID,
        overall_difficulty: DifficultyLevel,
        *,
        idempotency_key: str,
    ) -> ConfirmedUploadResult:
        return ConfirmedUploadResult(
            upload_id=upload_id,
            status=UploadStatus.PENDING,
            credit_state=CreditState.PROVISIONAL,
            overall_difficulty=overall_difficulty,
            accepted_at=datetime.now(UTC),
        )

    def ingest_upload(
        self,
        upload_id: UUID,
        attempt_id: UUID,
        *,
        config_snapshot: SnappingConfig,
    ) -> IngestionResult:
        return IngestionResult(
            track_id=str(upload_id),
            graph_revision=1,
            raw_point_count=2,
            reduced_point_count=2,
            nodes_created=2,
            nodes_reused=0,
            edges_created=1,
            edges_reused=0,
            self_loops_skipped=0,
            zero_length_edges_skipped=0,
            snapped_segments=((1, 2),),
            edges=(
                SnappedEdgeRef(
                    edge_id=1,
                    source_node_id=1,
                    target_node_id=2,
                    length_m=140.0,
                    created=True,
                ),
            ),
            elapsed_ms=4.2,
            db_queries_count=5,
        )

    def ingest_batch_file(
        self,
        batch_file_id: int,
        attempt_id: UUID,
        parsed_track: ParsedTrack,
        *,
        config_snapshot: SnappingConfig,
    ) -> IngestionResult:
        return self.ingest_upload(uuid4(), attempt_id, config_snapshot=config_snapshot)


class _DummyRatingService:
    def get_edge_rating(
        self,
        edge_id: int,
        *,
        user_id: int | None = None,
    ) -> EdgeRatingSummary:
        return EdgeRatingSummary(
            edge_id=edge_id,
            avg_difficulty=2.0,
            display_level=2,
            report_count=1,
            user_rating=2 if user_id is not None else None,
            graph_revision=3,
        )

    def upsert_edge_rating(
        self,
        edge_id: int,
        user_id: int,
        difficulty: DifficultyLevel,
    ) -> EdgeRatingSummary:
        return EdgeRatingSummary(
            edge_id=edge_id,
            avg_difficulty=float(difficulty),
            display_level=difficulty,
            report_count=1,
            user_rating=difficulty,
            graph_revision=4,
        )


class _DummyTrackSource:
    source_kind: Literal["cloud", "server"] = "server"

    def list_tracks(self) -> Sequence[TrackSourceEntry]:
        return (TrackSourceEntry(source_ref="trail1.gpx", filename="trail1.gpx", size_bytes=512),)

    def open_track_bytes(self, source_ref: str) -> bytes:
        return b"<gpx></gpx>"


class _DummyFileStorageAdapter:
    def put_durable(self, key: str, data: bytes) -> StoredFileRef:
        return StoredFileRef(key=key, size_bytes=len(data), sha256="0" * 64)

    def read_bytes(self, key: str) -> bytes:
        return b""

    def delete_if_exists(self, key: str) -> bool:
        return True

    def list_keys(self, *, prefix: str = "") -> Sequence[StoredFileInfo]:
        return (StoredFileInfo(key=f"{prefix}a.gpx", size_bytes=10, modified_at=datetime.now(UTC)),)


class _DummyGeocodingProvider:
    name: str = "fake-geocoder"

    def search(
        self,
        query: str,
        *,
        lang: PreferredLanguage | None = None,
        limit: int = 5,
    ) -> Sequence[GeocodingMatch]:
        return (
            GeocodingMatch(
                provider_place_id="haifa-1",
                display_name="Haifa, Israel" if lang == "en" else "חיפה, ישראל",
                lat=32.794,
                lng=34.9896,
                category="city",
            ),
        )


class _IncompleteStub:
    name: str = "incomplete"


# ---------------------------------------------------------------------------
# 1. Identity re-exports & package layout
# ---------------------------------------------------------------------------


def test_contracts_root_exports_frozen_contracts_after_t009() -> None:
    assert "CONTRACT_STAGE" in contracts_root_all
    assert "CONTRACT_VERSION" in contracts_root_all
    assert "ErrorResponse" in contracts_root_all
    assert "RoutingProvider" in contracts_root_all


def test_identity_reexports_have_zero_drift() -> None:
    assert ErrorResponse is CoreErrorResponse
    assert provisional.common.ErrorResponse is CoreErrorResponse

    assert HealthResponse is CoreHealthResponse
    assert provisional.schemas.HealthResponse is CoreHealthResponse

    assert PublicConfigResponse is CorePublicConfigResponse
    assert provisional.schemas.PublicConfigResponse is CorePublicConfigResponse

    assert RoutingProvider is CoreRoutingProvider
    assert provisional.protocols.RoutingProvider is CoreRoutingProvider


# ---------------------------------------------------------------------------
# 2. Runtime-checkable protocol conformance & strict GeocodingProvider split
# ---------------------------------------------------------------------------


def test_existing_routing_providers_conform_and_do_not_implement_geocoding() -> None:
    fake_provider = FakeRoutingProvider()
    assert isinstance(fake_provider, RoutingProvider)
    assert not isinstance(fake_provider, GeocodingProvider)

    with OpenRouteServiceProvider("test-ors-key") as ors_provider:
        assert isinstance(ors_provider, RoutingProvider)
        assert not isinstance(ors_provider, GeocodingProvider)

    with GraphHopperProvider("test-gh-key") as gh_provider:
        assert isinstance(gh_provider, RoutingProvider)
        assert not isinstance(gh_provider, GeocodingProvider)


def test_service_and_adapter_protocols_runtime_and_static_conformance() -> None:
    eligibility: EligibilityService = _DummyEligibilityService()
    ingestion: IngestionService = _DummyIngestionService()
    rating: RatingService = _DummyRatingService()
    track_source: TrackSource = _DummyTrackSource()
    storage: FileStorageAdapter = _DummyFileStorageAdapter()
    geocoder: GeocodingProvider = _DummyGeocodingProvider()

    assert isinstance(eligibility, EligibilityService)
    assert isinstance(ingestion, IngestionService)
    assert isinstance(rating, RatingService)
    assert isinstance(track_source, TrackSource)
    assert isinstance(storage, FileStorageAdapter)
    assert isinstance(geocoder, GeocodingProvider)

    # GeocodingProvider must remain strictly separated from RoutingProvider (FR-025).
    assert not isinstance(geocoder, RoutingProvider)

    # Smoke-call protocol methods to confirm DTO round-trips stay type-consistent.
    decision = eligibility.evaluate(1)
    assert decision.allowed is True
    assert rating.get_edge_rating(7, user_id=1).edge_id == 7
    assert track_source.source_kind == "server"
    assert storage.delete_if_exists("uploads/sample.gpx") is True
    assert len(geocoder.search("חיפה", lang="he", limit=3)) == 1


@pytest.mark.parametrize(
    "protocol_cls",
    [
        RoutingProvider,
        EligibilityService,
        IngestionService,
        RatingService,
        TrackSource,
        FileStorageAdapter,
        GeocodingProvider,
    ],
)
def test_incomplete_stub_does_not_satisfy_any_protocol(protocol_cls: type[Any]) -> None:
    assert not isinstance(_IncompleteStub(), protocol_cls)


# ---------------------------------------------------------------------------
# 3. derive_contribution_level boundaries (AC-101) & shared enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("active_count", "expected_level", "expected_key", "expected_next", "expected_to_next"),
    [
        (0, "Explorer", ContributionLevelKey.EXPLORER, "Adventurer", 6),
        (5, "Explorer", ContributionLevelKey.EXPLORER, "Adventurer", 1),
        (6, "Adventurer", ContributionLevelKey.ADVENTURER, "Trailblazer", 4),
        (9, "Adventurer", ContributionLevelKey.ADVENTURER, "Trailblazer", 1),
        (10, "Trailblazer", ContributionLevelKey.TRAILBLAZER, "Trail Master", 10),
        (19, "Trailblazer", ContributionLevelKey.TRAILBLAZER, "Trail Master", 1),
        (20, "Trail Master", ContributionLevelKey.TRAIL_MASTER, None, None),
        (35, "Trail Master", ContributionLevelKey.TRAIL_MASTER, None, None),
    ],
)
def test_derive_contribution_level_ac101_boundaries(
    active_count: int,
    expected_level: str,
    expected_key: ContributionLevelKey,
    expected_next: str | None,
    expected_to_next: int | None,
) -> None:
    info = derive_contribution_level(active_count)
    assert isinstance(info, ContributionLevelInfo)
    assert info.level == expected_level
    assert info.badge_key is expected_key
    assert info.active_count == active_count
    assert info.next_level == expected_next
    assert info.tracks_to_next == expected_to_next


@pytest.mark.parametrize("invalid_count", [-1, -100, True, False])
def test_derive_contribution_level_rejects_negative_and_bool(invalid_count: int) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        derive_contribution_level(invalid_count)


def test_credit_bearing_upload_statuses_match_spec() -> None:
    expected = frozenset(
        {
            UploadStatus.PENDING,
            UploadStatus.PROCESSING,
            UploadStatus.RETRY,
            UploadStatus.TECHNICAL_FAILED,
            UploadStatus.ACTIVE,
        }
    )
    assert isinstance(CREDIT_BEARING_UPLOAD_STATUSES, frozenset)
    assert expected == CREDIT_BEARING_UPLOAD_STATUSES
    assert UploadStatus.DRAFT not in CREDIT_BEARING_UPLOAD_STATUSES
    assert UploadStatus.REJECTED not in CREDIT_BEARING_UPLOAD_STATUSES


def test_coordinate_schema_validation_and_domain_roundtrip() -> None:
    schema = CoordinateSchema(lat=32.794, lng=34.9896, ele=120.5)
    coord = schema.to_coordinate()
    assert coord == Coordinate(lat=32.794, lng=34.9896, ele=120.5)
    assert CoordinateSchema.from_coordinate(coord) == schema

    for bad_payload in [
        {"lat": 90.1, "lng": 34.9},
        {"lat": -90.1, "lng": 34.9},
        {"lat": 32.0, "lng": 180.1},
        {"lat": 32.0, "lng": -180.1},
        {"lat": float("nan"), "lng": 34.9},
        {"lat": float("inf"), "lng": 34.9},
        {"lat": 32.0, "lng": 34.9, "ele": float("-inf")},
        {"lat": True, "lng": 34.9},
        {"lat": 32.0, "lng": False},
        {"lat": 32.0, "lng": 34.9, "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            CoordinateSchema.model_validate(bad_payload)


# ---------------------------------------------------------------------------
# 4. Mutation request schemas & ConfigDict(extra="forbid")
# ---------------------------------------------------------------------------


def _valid_register_payload() -> dict[str, Any]:
    return {
        "first_name": "Eyal",
        "last_name": "Cohen",
        "email": "eyal@example.com",
        "username": "EyalRider",
        "password": "Pass1!",
    }


def _valid_admin_config_payload() -> dict[str, Any]:
    return {
        "min_required_tracks": 1,
        "point_simplification_distance_m": 20.0,
        "node_snap_radius_m": 20.0,
        "free_route_creations": 0,
        "rejected_track_grace_period_hours": 24.0,
    }


def _valid_route_request_payload() -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "start_lat": 32.794,
        "start_lng": 34.9896,
        "end_lat": 32.810,
        "end_lng": 35.010,
    }


@pytest.mark.parametrize(
    ("model_cls", "base_payload"),
    [
        (RegisterRequest, _valid_register_payload()),
        (LoginRequest, {"username": "EyalRider", "password": "Pass1!"}),
        (LogoutRequest, {"expected_session_id": str(uuid4())}),
        (UserPreferencesPatchRequest, {"preferred_language": "en"}),
        (RouteCreateRequest, _valid_route_request_payload()),
        (UploadConfirmRequest, {"overall_difficulty": 2}),
        (EdgeRatingPutRequest, {"difficulty": 3}),
        (AdminConfigUpdateRequest, _valid_admin_config_payload()),
        (BatchImportStartRequest, {"source": "server"}),
    ],
)
def test_all_mutation_request_models_forbid_extra_fields(
    model_cls: type[BaseModel],
    base_payload: dict[str, Any],
) -> None:
    assert model_cls.model_config.get("extra") == "forbid"
    assert model_cls.model_validate(base_payload) is not None

    with pytest.raises(ValidationError) as exc_info:
        model_cls.model_validate({**base_payload, "injected_field": "malicious"})
    assert any(err["type"] == "extra_forbidden" for err in exc_info.value.errors())


def test_upload_confirm_request_ac55_rejects_injected_coordinates() -> None:
    for injected in [
        {"overall_difficulty": 2, "coordinates": [[34.98, 32.79], [34.99, 32.80]]},
        {"overall_difficulty": 2, "points": [{"lat": 32.79, "lng": 34.98}]},
        {"overall_difficulty": 2, "segments": []},
        {"overall_difficulty": 2, "original_ref": "uploads/other.gpx"},
    ]:
        with pytest.raises(ValidationError) as exc_info:
            UploadConfirmRequest.model_validate(injected)
        assert any(err["type"] == "extra_forbidden" for err in exc_info.value.errors())


@pytest.mark.parametrize("difficulty", [1, 2, 3])
def test_upload_confirm_and_edge_rating_accept_valid_difficulty(difficulty: int) -> None:
    confirm = UploadConfirmRequest.model_validate({"overall_difficulty": difficulty})
    rating = EdgeRatingPutRequest.model_validate({"difficulty": difficulty})
    assert confirm.overall_difficulty == difficulty
    assert rating.difficulty == difficulty


@pytest.mark.parametrize("bad_value", [0, 4, -1, None, True, False, 1.5, 2.0, "2"])
def test_upload_confirm_and_edge_rating_ac56_reject_invalid_and_bool_difficulty(
    bad_value: Any,
) -> None:
    with pytest.raises(ValidationError):
        UploadConfirmRequest.model_validate({"overall_difficulty": bad_value})
    with pytest.raises(ValidationError):
        EdgeRatingPutRequest.model_validate({"difficulty": bad_value})


def test_register_request_default_language_and_valid_boundaries() -> None:
    req = RegisterRequest.model_validate(_valid_register_payload())
    assert req.preferred_language == DEFAULT_PREFERRED_LANGUAGE == "he"

    req_en = RegisterRequest.model_validate(
        {
            "first_name": "A" * 16,
            "last_name": "B",
            "email": "a@b.co",
            "username": "abcd",
            "password": "Aa1@",
            "preferred_language": "en",
        }
    )
    assert req_en.preferred_language == "en"

    req_max = RegisterRequest.model_validate(
        {
            "first_name": " Eyal ",
            "last_name": " Cohen ",
            "email": f"{'x' * 40}@domain.co",
            "username": "A" * 32,
            "password": "Aa1?567890123456",
        }
    )
    assert req_max.first_name == "Eyal"
    assert req_max.last_name == "Cohen"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        # AC-2 strict first/last name rules (1..16 non-whitespace)
        ("first_name", ""),
        ("first_name", "   "),
        ("first_name", "A" * 17),
        ("last_name", ""),
        ("last_name", "   "),
        ("last_name", "B" * 17),
        # Email structure and max 50 chars
        ("email", "not-an-email"),
        ("email", "user@nodot"),
        ("email", "user@.com"),
        ("email", "user@domain."),
        ("email", "user@a..b.com"),
        ("email", "user@a@b.com"),
        ("email", "user name@domain.com"),
        ("email", f"{'a' * 45}@b.com"),
        # AC-2 username: 4..32 English letters only
        ("username", "abc"),
        ("username", "a" * 33),
        ("username", "user123"),
        ("username", "user_name"),
        ("username", "אבגדה"),
        # AC-2 password: 4..16 chars, [A-Za-z0-9@$!%*?&], all 4 classes
        ("password", "Aa1"),
        ("password", "Aa1@" + "a" * 13),
        ("password", "aa1@"),
        ("password", "AA1@"),
        ("password", "Aa@@"),
        ("password", "Aa12"),
        ("password", "Aa1#"),
        ("password", "Aa1 @"),
        # Language literal
        ("preferred_language", "fr"),
    ],
)
def test_register_request_ac2_rejects_invalid_fields(field: str, bad_value: Any) -> None:
    payload = _valid_register_payload()
    payload[field] = bad_value
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(payload)


def test_login_and_logout_request_validation() -> None:
    assert LogoutRequest().expected_session_id is None
    sid = uuid4()
    logout = LogoutRequest.model_validate({"expected_session_id": str(sid)})
    assert logout.expected_session_id == sid

    with pytest.raises(ValidationError):
        LogoutRequest.model_validate({"expected_session_id": "not-a-uuid"})

    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"username": "   ", "password": "secret"})

    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"username": "Eyal", "password": ""})


def test_route_create_request_validation() -> None:
    payload = _valid_route_request_payload()
    req = RouteCreateRequest.model_validate(payload)
    assert isinstance(req.request_id, UUID)
    assert req.include_diagnostics is False

    for field, bad_value in [
        ("request_id", "not-a-uuid"),
        ("start_lat", 90.01),
        ("start_lat", -90.01),
        ("start_lng", 180.01),
        ("start_lng", -180.01),
        ("end_lat", float("nan")),
        ("end_lat", float("inf")),
        ("end_lng", float("-inf")),
        ("start_lat", True),
        ("end_lng", False),
    ]:
        bad_payload = {**payload, field: bad_value}
        with pytest.raises(ValidationError):
            RouteCreateRequest.model_validate(bad_payload)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        # AC-47
        ("min_required_tracks", 0),
        ("min_required_tracks", -1),
        ("min_required_tracks", "abc"),
        ("min_required_tracks", True),
        ("min_required_tracks", False),
        ("free_route_creations", -1),
        ("free_route_creations", -2),
        ("free_route_creations", "abc"),
        ("free_route_creations", True),
        ("free_route_creations", False),
        # AC-77 positive finite metric settings
        ("point_simplification_distance_m", 0),
        ("point_simplification_distance_m", -5.0),
        ("point_simplification_distance_m", float("nan")),
        ("point_simplification_distance_m", float("inf")),
        ("point_simplification_distance_m", float("-inf")),
        ("point_simplification_distance_m", "abc"),
        ("point_simplification_distance_m", True),
        ("node_snap_radius_m", 0),
        ("node_snap_radius_m", -1.0),
        ("node_snap_radius_m", float("nan")),
        ("node_snap_radius_m", float("inf")),
        ("node_snap_radius_m", "abc"),
        ("node_snap_radius_m", False),
        # Non-negative finite grace period
        ("rejected_track_grace_period_hours", -0.1),
        ("rejected_track_grace_period_hours", float("nan")),
        ("rejected_track_grace_period_hours", float("inf")),
        ("rejected_track_grace_period_hours", True),
    ],
)
def test_admin_config_update_request_ac47_ac77_bounds(field: str, bad_value: Any) -> None:
    payload = _valid_admin_config_payload()
    payload[field] = bad_value
    with pytest.raises(ValidationError):
        AdminConfigUpdateRequest.model_validate(payload)


def test_admin_config_update_allows_zero_grace_and_independent_metrics() -> None:
    cfg = AdminConfigUpdateRequest.model_validate(
        {
            "min_required_tracks": 2,
            "point_simplification_distance_m": 20.0,
            "node_snap_radius_m": 10.0,
            "free_route_creations": 0,
            "rejected_track_grace_period_hours": 0.0,
        }
    )
    assert cfg.point_simplification_distance_m == 20.0
    assert cfg.node_snap_radius_m == 10.0
    assert cfg.rejected_track_grace_period_hours == 0.0


def test_admin_user_schema_ac42_redacts_password_hash_and_created_at() -> None:
    assert set(AdminUserSchema.model_fields) == {
        "id",
        "username",
        "first_name",
        "last_name",
        "email",
        "user_type",
    }
    assert "password_hash" not in AdminUserSchema.model_fields
    assert "created_at" not in AdminUserSchema.model_fields

    valid_user = {
        "id": 1,
        "username": "AdminUser",
        "first_name": "Eyal",
        "last_name": "Cohen",
        "email": "admin@example.com",
        "user_type": UserRole.SYSTEM,
    }
    parsed = AdminUserSchema.model_validate(valid_user)
    dumped = parsed.model_dump()
    assert "password_hash" not in dumped
    assert "created_at" not in dumped

    for forbidden in ("password_hash", "created_at"):
        with pytest.raises(ValidationError):
            AdminUserSchema.model_validate({**valid_user, forbidden: "leaked"})


def test_route_segment_and_response_schemas_enforce_invariants() -> None:
    p1 = CoordinateSchema(lat=32.794, lng=34.9896)
    p2 = CoordinateSchema(lat=32.795, lng=34.9906)

    net_seg = RouteSegmentSchema(
        source="network",
        points=[p1, p2],
        edge_id=42,
        avg_difficulty=2.5,
        display_level=3,
    )
    ext_seg = RouteSegmentSchema(
        source="external",
        points=[p1, p2],
        provider_profile="cycling-mountain",
    )
    resp = RouteCreateResponse(
        request_id=uuid4(),
        segments=[net_seg, ext_seg],
        diagnostics=RouteDiagnosticsSchema(
            branch="connected",
            algorithm="pgr_dijkstra",
            cost_mode="distance",
            entry_node_id=1,
            exit_node_id=2,
            entry_distance_m=150.0,
            exit_distance_m=220.0,
            graph_revision=10,
            internal_query_ms=3.5,
            external_latency_ms=45.0,
        ),
    )
    assert len(resp.segments) == 2

    # Network segment requires edge_id and forbids provider_profile
    with pytest.raises(ValidationError):
        RouteSegmentSchema(source="network", points=[p1, p2])
    with pytest.raises(ValidationError):
        RouteSegmentSchema(
            source="network",
            points=[p1, p2],
            edge_id=1,
            provider_profile="cycling-mountain",
        )

    # External segment requires provider_profile and forbids edge_id or trail difficulty
    with pytest.raises(ValidationError):
        RouteSegmentSchema(source="external", points=[p1, p2])
    with pytest.raises(ValidationError):
        RouteSegmentSchema(
            source="external",
            points=[p1, p2],
            edge_id=1,
            provider_profile="cycling-mountain",
        )
    with pytest.raises(ValidationError):
        RouteSegmentSchema(
            source="external",
            points=[p1, p2],
            provider_profile="cycling-mountain",
            avg_difficulty=2.0,
        )
    with pytest.raises(ValidationError):
        RouteSegmentSchema(
            source="external",
            points=[p1, p2],
            provider_profile="cycling-mountain",
            display_level=2,
        )


def test_remaining_response_and_geojson_schemas_smoke() -> None:
    now = datetime.now(UTC)
    level = derive_contribution_level(6)
    entitlement = EntitlementResponse(
        allowed=True,
        reason=None,
        admin_exempt=False,
        contribution_eligible=True,
        qualifying_count=6,
        min_required_tracks=1,
        free_allowance_limit=0,
        free_allowance_remaining=0,
        grace_until=None,
        contribution_level=level,
    )

    me = AuthMeResponse(
        id=1,
        username="EyalRider",
        first_name="Eyal",
        last_name="Cohen",
        email="eyal@example.com",
        user_type=UserRole.REGULAR,
        session_id=uuid4(),
        contribution_level=level,
    )
    assert me.preferred_language == "he"

    validate_resp = UploadValidateResponse(
        upload_id=uuid4(),
        filename="carmel.gpx",
        point_count=2,
        segments=[
            UploadPreviewSegmentSchema(
                segment_index=0,
                points=[
                    CoordinateSchema(lat=32.79, lng=34.98),
                    CoordinateSchema(lat=32.80, lng=34.99),
                ],
            )
        ],
        expires_at=now,
    )
    assert validate_resp.status == UploadStatus.DRAFT
    assert validate_resp.credit_state == CreditState.NONE

    confirm_resp = UploadConfirmResponse(
        upload_id=validate_resp.upload_id,
        filename="carmel.gpx",
        status=UploadStatus.PENDING,
        credit_state=CreditState.PROVISIONAL,
        overall_difficulty=2,
        accepted_at=now,
        entitlement=entitlement,
    )
    assert confirm_resp.overall_difficulty == 2

    uploads_resp = MyUploadsResponse(
        uploads=[
            MyUploadItemSchema(
                upload_id=validate_resp.upload_id,
                filename="carmel.gpx",
                status=UploadStatus.REJECTED,
                credit_state=CreditState.NONE,
                overall_difficulty=2,
                rejection_code=RejectionCode.DEGENERATE_GEOMETRY,
                safe_error_message="Track geometry is degenerate.",
                submitted_at=now,
                processed_at=now,
            )
        ],
        entitlement=entitlement,
        contribution_level=level,
    )
    assert uploads_resp.uploads[0].rejection_code == RejectionCode.DEGENERATE_GEOMETRY

    viewport = NetworkEdgesViewportResponse(
        edges=[
            NetworkEdgeSchema(
                edge_id=10,
                points=[
                    CoordinateSchema(lat=32.79, lng=34.98),
                    CoordinateSchema(lat=32.80, lng=34.99),
                ],
                length_m=1200.0,
                avg_difficulty=2.0,
                display_level=2,
                report_count=3,
            )
        ],
        truncated=False,
        graph_revision=7,
    )
    assert viewport.truncated is False

    rating_resp = EdgeRatingResponse(
        edge_id=10,
        avg_difficulty=2.25,
        display_level=2,
        report_count=4,
        user_rating=3,
        graph_revision=8,
    )
    assert rating_resp.user_rating == 3

    search_resp = LocationSearchResponse(
        query="חיפה",
        results=[
            LocationSearchResultSchema(
                place_id="haifa-1",
                display_name="חיפה",
                lat=32.794,
                lng=34.9896,
            )
        ],
    )
    assert len(search_resp.results) == 1

    admin_users = AdminUsersResponse(users=[])
    assert admin_users.users == []

    admin_cfg = AdminConfigResponse(**_valid_admin_config_payload())
    assert admin_cfg.min_required_tracks == 1

    batch_status = BatchImportStatusResponse(
        batch_id=uuid4(),
        source="cloud",
        status=BatchImportStatus.QUEUED,
        total_files=12,
        imported_count=0,
        skipped_count=0,
        failed_count=0,
        started_at=now,
    )
    assert batch_status.finished_at is None

    geojson = AdminNetworkGeoJsonResponse(
        graph_revision=8,
        features=[
            AdminNetworkEdgeFeature(
                id=10,
                geometry=GeoJsonLineStringGeometry(
                    coordinates=[(34.98, 32.79), (34.99, 32.80)],
                ),
                properties=AdminNetworkEdgeProperties(
                    edge_id=10,
                    length_m=1200.0,
                    avg_difficulty=2.0,
                    display_level=2,
                    report_count=3,
                ),
            ),
            AdminNetworkNodeFeature(
                id=1,
                geometry=GeoJsonPointGeometry(coordinates=(34.98, 32.79, 250.0)),
                properties=AdminNetworkNodeProperties(node_id=1, ele=250.0),
            ),
        ],
    )
    assert geojson.type == "FeatureCollection"

    # GeoJSON out-of-bounds validation
    with pytest.raises(ValidationError):
        GeoJsonLineStringGeometry(coordinates=[(181.0, 32.79), (34.99, 32.80)])
    with pytest.raises(ValidationError):
        GeoJsonPointGeometry(coordinates=(34.98, 95.0))
    with pytest.raises(ValidationError):
        GeoJsonPointGeometry(coordinates=(34.98, 32.79, float("nan")))

    # Sanity-check error & entitlement denial codes exist
    assert ErrorCode.ROUTE_NOT_ALLOWED == "ROUTE_NOT_ALLOWED"
    assert EntitlementDenialReason.BELOW_THRESHOLD == "BELOW_THRESHOLD"


# ---------------------------------------------------------------------------
# 5. Policy registries & provisional stage metadata
# ---------------------------------------------------------------------------


def test_policy_stage_and_registries() -> None:
    assert CONTRACT_STAGE == "provisional"
    assert CONTRACT_FREEZE_TASK == "T-009"
    assert is_provisional_contract() is True

    expected_gate_1_keys = {
        "free_route_creations",
        "rejection_grace_period_hours",
        "in_flight_entitlement_changes",
        "admin_routing_exemption",
        "same_user_duplicate_fingerprint",
        "cross_user_duplicates",
        "zero_new_edge_credit",
        "one_editable_rating_per_user_edge",
        "initial_difficulty_excluded_from_mean",
        "rating_without_route_entitlement",
        "password_policy",
        "supported_upload_formats",
        "inactive_user_access",
    }
    assert set(GATE_1_POLICY_ANNOTATIONS.keys()) == expected_gate_1_keys

    expected_gate_2_keys = {
        "routing_cost_mode",
        "point_simplification_distance_m",
        "node_snap_radius_m",
        "routing_provider",
        "geocoding_provider",
        "contribution_level_labels",
        "business_rejection_codes",
        "routing_fallback_profile",
    }
    assert set(GATE_2_OPEN_FIELDS.keys()) == expected_gate_2_keys
    assert set(GATE_1_POLICY_ANNOTATIONS).isdisjoint(GATE_2_OPEN_FIELDS)

    for key, annotation in GATE_1_POLICY_ANNOTATIONS.items():
        assert isinstance(annotation, PolicyAnnotation)
        assert annotation.key == key
        assert annotation.gate == "gate_1"
        assert annotation.status is PolicyStatus.GATE_1_CONFIRMED
        assert annotation.topic
        assert annotation.current_default
        assert annotation.affected_contracts
        assert annotation.source_ref
        assert annotation.notes

    for key, annotation in GATE_2_OPEN_FIELDS.items():
        assert isinstance(annotation, PolicyAnnotation)
        assert annotation.key == key
        assert annotation.gate == "gate_2"
        assert annotation.status is PolicyStatus.GATE_2_OPEN
        assert annotation.topic
        assert annotation.current_default
        assert annotation.affected_contracts
        assert annotation.source_ref
        assert annotation.notes

    # Both registries are read-only MappingProxyType instances.
    with pytest.raises(TypeError):
        GATE_1_POLICY_ANNOTATIONS["injected"] = next(  # type: ignore[index]
            iter(GATE_1_POLICY_ANNOTATIONS.values())
        )
    with pytest.raises(TypeError):
        GATE_2_OPEN_FIELDS["injected"] = next(  # type: ignore[index]
            iter(GATE_2_OPEN_FIELDS.values())
        )
