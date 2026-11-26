"""Unit tests for frozen T-009 contracts, security hardening, OpenAPI spec, and codegen."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.contracts as contracts
from app.contracts import (
    CONTRACT_FREEZE_TASK,
    CONTRACT_STAGE,
    CONTRACT_VERSION,
    MAX_FREE_ROUTE_CREATIONS,
    MAX_MIN_REQUIRED_TRACKS,
    MAX_NODE_SNAP_RADIUS_M,
    MAX_POINT_SIMPLIFICATION_DISTANCE_M,
    MAX_REJECTED_TRACK_GRACE_PERIOD_HOURS,
    AdminConfigUpdateRequest,
    AdminNetworkEdgeFeature,
    AdminNetworkEdgeProperties,
    AdminNetworkEdgesGeoJsonResponse,
    AdminNetworkGeoJsonResponse,
    AdminNetworkNodeFeature,
    AdminNetworkNodeProperties,
    AdminNetworkNodesGeoJsonResponse,
    CreditState,
    ErrorCode,
    ErrorResponse,
    GeocodingProvider,
    GeoJsonLineStringGeometry,
    GeoJsonPointGeometry,
    HealthResponse,
    IngestionRunStatus,
    IngestionRunSummarySchema,
    LoginRequest,
    MyUploadItemSchema,
    PublicConfigResponse,
    RegisterRequest,
    RouteCreateRequest,
    RoutingProvider,
    UploadStatus,
    derive_contribution_level,
    is_provisional_contract,
    provisional,
)
from app.contracts.codegen import (
    check_artifacts,
    default_repo_root,
    generate_all,
    render_openapi_json,
    write_artifacts,
)
from app.contracts.codegen import (
    main as codegen_main,
)
from app.contracts.openapi_app import create_contract_openapi_app, export_openapi_schema
from app.core.errors import ErrorResponse as CoreErrorResponse
from app.health.schemas import HealthResponse as CoreHealthResponse
from app.health.schemas import PublicConfigResponse as CorePublicConfigResponse
from app.integrations.routing.protocol import RoutingProvider as CoreRoutingProvider
from app.integrations.routing.providers import FakeRoutingProvider


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


# ---------------------------------------------------------------------------
# 1. Frozen stage metadata, identity re-exports & provisional compatibility
# ---------------------------------------------------------------------------


def test_frozen_stage_and_provisional_backwards_compatibility() -> None:
    assert CONTRACT_STAGE == "frozen"
    assert CONTRACT_VERSION == "1.0.0"
    assert CONTRACT_FREEZE_TASK == "T-009"
    assert is_provisional_contract() is False

    # Provisional policy namespace stays explicitly provisional for backwards compatibility.
    assert provisional.policy.CONTRACT_STAGE == "provisional"
    assert provisional.policy.is_provisional_contract() is True
    assert provisional.GATE_1_POLICY_ANNOTATIONS is contracts.GATE_1_POLICY_ANNOTATIONS
    assert provisional.GATE_2_OPEN_FIELDS is contracts.GATE_2_OPEN_FIELDS

    # Zero-drift identity re-exports across root and provisional namespaces.
    assert ErrorResponse is CoreErrorResponse
    assert HealthResponse is CoreHealthResponse
    assert PublicConfigResponse is CorePublicConfigResponse
    assert RoutingProvider is CoreRoutingProvider
    assert provisional.ErrorResponse is CoreErrorResponse
    assert provisional.HealthResponse is CoreHealthResponse
    assert provisional.PublicConfigResponse is CorePublicConfigResponse
    assert provisional.RoutingProvider is CoreRoutingProvider

    # Strict GeocodingProvider vs RoutingProvider split preserved.
    fake_router = FakeRoutingProvider()
    assert isinstance(fake_router, RoutingProvider)
    assert not isinstance(fake_router, GeocodingProvider)

    # Every shared symbol from provisional resolves to the exact frozen symbol.
    provisional_only_overrides = {"CONTRACT_STAGE", "is_provisional_contract"}
    for name in provisional.__all__:
        if name in provisional_only_overrides:
            continue
        assert getattr(provisional, name) is getattr(contracts, name), f"Drift in {name}"


def test_frozen_refinements_ingestion_run_and_typed_geojson_splits() -> None:
    now = datetime.now(UTC)
    run = IngestionRunSummarySchema(
        id=uuid4(),
        source_type="upload",
        status=IngestionRunStatus.COMPLETED,
        track_id="sha256:abc123",
        graph_revision_before=4,
        created_at=now,
        completed_at=now,
    )
    upload_item = MyUploadItemSchema(
        upload_id=uuid4(),
        filename="carmel.gpx",
        status=UploadStatus.ACTIVE,
        credit_state=CreditState.PERMANENT,
        overall_difficulty=2,
        submitted_at=now,
        processed_at=now,
        latest_ingestion_run=run,
    )
    assert upload_item.latest_ingestion_run is not None
    assert upload_item.latest_ingestion_run.status is IngestionRunStatus.COMPLETED
    assert derive_contribution_level(20).level == "Trail Master"

    edge_feature = AdminNetworkEdgeFeature(
        id=10,
        geometry=GeoJsonLineStringGeometry(coordinates=[(34.98, 32.79), (34.99, 32.80)]),
        properties=AdminNetworkEdgeProperties(
            edge_id=10,
            length_m=1500.0,
            avg_difficulty=2.0,
            display_level=2,
            report_count=2,
        ),
    )
    node_feature = AdminNetworkNodeFeature(
        id=1,
        geometry=GeoJsonPointGeometry(coordinates=(34.98, 32.79, 250.0)),
        properties=AdminNetworkNodeProperties(node_id=1, ele=250.0),
    )

    edges_fc = AdminNetworkEdgesGeoJsonResponse(graph_revision=5, features=[edge_feature])
    nodes_fc = AdminNetworkNodesGeoJsonResponse(graph_revision=5, features=[node_feature])
    mixed_fc = AdminNetworkGeoJsonResponse(graph_revision=5, features=[edge_feature, node_feature])
    assert len(edges_fc.features) == 1
    assert len(nodes_fc.features) == 1
    assert len(mixed_fc.features) == 2

    with pytest.raises(ValidationError):
        AdminNetworkEdgesGeoJsonResponse.model_validate(
            {"graph_revision": 5, "features": [node_feature.model_dump()]}
        )
    with pytest.raises(ValidationError):
        AdminNetworkNodesGeoJsonResponse.model_validate(
            {"graph_revision": 5, "features": [edge_feature.model_dump()]}
        )


# ---------------------------------------------------------------------------
# 2. QA Carry-Forward Security Hardening (QA_REPORT.md §4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("control_char", ["\x00", "\x01", "\t", "\n", "\r", "\x1f", "\x7f"])
@pytest.mark.parametrize(
    "field",
    ["first_name", "last_name", "email", "username", "password"],
)
def test_register_request_rejects_nul_and_ascii_control_chars(
    field: str,
    control_char: str,
) -> None:
    payload = _valid_register_payload()
    if field == "email":
        payload[field] = f"eyal{control_char}@example.com"
    elif field == "password":
        payload[field] = f"Pass1!{control_char}"
    else:
        payload[field] = f"Eyal{control_char}Name"

    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(payload)


@pytest.mark.parametrize("control_char", ["\x00", "\x01", "\t", "\n", "\r", "\x1f", "\x7f"])
@pytest.mark.parametrize("field", ["username", "password"])
def test_login_request_rejects_nul_and_ascii_control_chars(
    field: str,
    control_char: str,
) -> None:
    payload = {"username": "EyalRider", "password": "Pass1!"}
    payload[field] = f"valid{control_char}value"
    with pytest.raises(ValidationError):
        LoginRequest.model_validate(payload)


def test_admin_config_update_enforces_finite_upper_bounds() -> None:
    # Exact upper bounds are accepted.
    at_max = AdminConfigUpdateRequest.model_validate(
        {
            "min_required_tracks": MAX_MIN_REQUIRED_TRACKS,
            "free_route_creations": MAX_FREE_ROUTE_CREATIONS,
            "point_simplification_distance_m": MAX_POINT_SIMPLIFICATION_DISTANCE_M,
            "node_snap_radius_m": MAX_NODE_SNAP_RADIUS_M,
            "rejected_track_grace_period_hours": MAX_REJECTED_TRACK_GRACE_PERIOD_HOURS,
        }
    )
    assert at_max.min_required_tracks == 10_000
    assert at_max.free_route_creations == 10_000
    assert at_max.point_simplification_distance_m == 10_000.0
    assert at_max.node_snap_radius_m == 10_000.0
    assert at_max.rejected_track_grace_period_hours == 8760.0

    # Values exceeding any upper bound are rejected (preventing timedelta overflow).
    for field, over_max in [
        ("min_required_tracks", 10_001),
        ("free_route_creations", 10_001),
        ("point_simplification_distance_m", 10_000.1),
        ("node_snap_radius_m", 10_000.1),
        ("rejected_track_grace_period_hours", 8760.1),
        ("rejected_track_grace_period_hours", 1e12),
    ]:
        payload = _valid_admin_config_payload()
        payload[field] = over_max
        with pytest.raises(ValidationError):
            AdminConfigUpdateRequest.model_validate(payload)


def test_route_create_request_documents_system_role_enforcement_on_include_diagnostics() -> None:
    field_info = RouteCreateRequest.model_fields["include_diagnostics"]
    assert field_info.default is False
    assert field_info.description is not None
    assert "system" in field_info.description.lower()
    assert "rbac" in field_info.description.lower()


# ---------------------------------------------------------------------------
# 3. OpenAPI 23-operation completeness, parameters, headers & zero drift
# ---------------------------------------------------------------------------


EXPECTED_OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("post", "/api/auth/register"): ("authRegister", "201"),
    ("post", "/api/auth/login"): ("authLogin", "200"),
    ("post", "/api/auth/logout"): ("authLogout", "200"),
    ("get", "/api/auth/me"): ("authMe", "200"),
    ("patch", "/api/me/preferences"): ("patchMyPreferences", "200"),
    ("get", "/api/health"): ("getHealth", "200"),
    ("get", "/api/config/public"): ("getPublicConfig", "200"),
    ("get", "/api/map/search"): ("searchMapLocations", "200"),
    ("get", "/api/me/entitlement"): ("getMyEntitlement", "200"),
    ("post", "/api/routes"): ("createRoute", "200"),
    ("post", "/api/uploads/validate"): ("validateUpload", "200"),
    ("post", "/api/uploads/{id}/confirm"): ("confirmUpload", "202"),
    ("delete", "/api/uploads/{id}"): ("deleteUploadDraft", "200"),
    ("get", "/api/me/uploads"): ("getMyUploads", "200"),
    ("get", "/api/admin/config"): ("getAdminConfig", "200"),
    ("put", "/api/admin/config"): ("updateAdminConfig", "200"),
    ("get", "/api/admin/users"): ("getAdminUsers", "200"),
    ("get", "/api/admin/network/edges"): ("getAdminNetworkEdges", "200"),
    ("get", "/api/admin/network/nodes"): ("getAdminNetworkNodes", "200"),
    ("get", "/api/network/edges"): ("getNetworkEdges", "200"),
    ("get", "/api/network/edges/{id}/rating"): ("getEdgeRating", "200"),
    ("put", "/api/network/edges/{id}/rating"): ("putEdgeRating", "200"),
    ("post", "/api/admin/imports"): ("startAdminBatchImport", "202"),
}


def test_openapi_spec_covers_all_23_arch_section_8_operations_and_params() -> None:
    app = create_contract_openapi_app()
    assert app.version == CONTRACT_VERSION

    schema = export_openapi_schema()
    paths = schema["paths"]

    actual_operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                actual_operations[(method, path)] = operation

    assert len(actual_operations) == 23
    assert set(actual_operations.keys()) == set(EXPECTED_OPERATIONS.keys())

    seen_op_ids: set[str] = set()
    for key, (expected_op_id, expected_status) in EXPECTED_OPERATIONS.items():
        op = actual_operations[key]
        assert op["operationId"] == expected_op_id
        assert expected_op_id not in seen_op_ids
        seen_op_ids.add(expected_op_id)
        assert op.get("tags"), f"Missing tags on {key}"
        assert expected_status in op["responses"], f"Missing {expected_status} on {key}"

        # Every error status code (4xx/5xx) must reference ErrorResponse.
        for resp_code, resp_obj in op["responses"].items():
            if str(resp_code).startswith(("4", "5")):
                json_schema = resp_obj["content"]["application/json"]["schema"]
                assert json_schema == {"$ref": "#/components/schemas/ErrorResponse"}, (
                    f"Unexpected error schema on {key} status {resp_code}: {json_schema}"
                )

    # Verify unused FastAPI default validation schemas were pruned and ErrorCode is exported.
    component_schemas = schema["components"]["schemas"]
    assert "HTTPValidationError" not in component_schemas
    assert "ValidationError" not in component_schemas
    assert set(component_schemas["ErrorCode"]["enum"]) == {code.value for code in ErrorCode}

    # Verify multipart/form-data binary file upload schema on POST /api/uploads/validate.
    validate_body = actual_operations[("post", "/api/uploads/validate")]["requestBody"]
    assert validate_body["required"] is True
    file_schema = validate_body["content"]["multipart/form-data"]["schema"]["properties"]["file"]
    assert file_schema["type"] == "string"
    assert file_schema["format"] == "binary"

    # Verify Idempotency-Key header on confirmUpload.
    confirm_params = {
        (p["in"], p["name"]): p
        for p in actual_operations[("post", "/api/uploads/{id}/confirm")]["parameters"]
    }
    assert ("header", "Idempotency-Key") in confirm_params
    assert confirm_params[("header", "Idempotency-Key")]["required"] is True
    assert ("path", "id") in confirm_params

    # Verify If-None-Match header + 304 response on both admin network endpoints.
    for admin_path in ("/api/admin/network/edges", "/api/admin/network/nodes"):
        op = actual_operations[("get", admin_path)]
        params = {(p["in"], p["name"]): p for p in op["parameters"]}
        assert ("header", "If-None-Match") in params
        assert params[("header", "If-None-Match")]["required"] is False
        assert "304" in op["responses"]

    # Verify map search query params (q, lang, limit).
    search_params = {
        (p["in"], p["name"]): p for p in actual_operations[("get", "/api/map/search")]["parameters"]
    }
    assert ("query", "q") in search_params and search_params[("query", "q")]["required"] is True
    assert ("query", "lang") in search_params
    assert ("query", "limit") in search_params

    # Verify viewport network edges query params (bbox, zoom).
    viewport_params = {
        (p["in"], p["name"]): p
        for p in actual_operations[("get", "/api/network/edges")]["parameters"]
    }
    assert ("query", "bbox") in viewport_params
    assert ("query", "zoom") in viewport_params


# ---------------------------------------------------------------------------
# 4. Codegen determinism & committed artifact drift checks
# ---------------------------------------------------------------------------


def test_codegen_is_deterministic_and_committed_artifacts_have_zero_drift(tmp_path: Path) -> None:
    first = generate_all()
    second = generate_all()
    assert first == second
    assert first.openapi_json.endswith("\n")

    # OpenAPI JSON is deterministically sorted and matches fresh export.
    assert first.openapi_json == render_openapi_json(export_openapi_schema())
    parsed = json.loads(first.openapi_json)
    assert parsed["info"]["version"] == CONTRACT_VERSION

    # Committed repository files match freshly generated artifacts with zero drift.
    repo_root = default_repo_root()
    assert check_artifacts(repo_root) == []
    assert codegen_main(["--check", "--repo-root", str(repo_root)]) == 0

    # Writing to an isolated root produces identical drift-free files.
    written = write_artifacts(tmp_path)
    assert len(written) == 4
    assert check_artifacts(tmp_path) == []
    assert codegen_main(["--check", "--repo-root", str(tmp_path)]) == 0
