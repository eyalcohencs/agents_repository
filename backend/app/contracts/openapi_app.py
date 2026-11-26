"""Pure schema-only FastAPI contract application for deterministic OpenAPI export (T-009).

This module defines zero database, storage, or runtime wiring and never touches
``app.main``. Its sole purpose is to materialize all 20 ``docs/ARCHITECTURE.md`` §8
API rows (23 HTTP operations) with explicit operation IDs, tags, status codes,
parameters, headers, request payloads, response models, and typed ``ErrorResponse``
error entries for deterministic ``backend/openapi.json`` generation.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import FastAPI, Header, Path, Query, status
from pydantic import TypeAdapter

from app.contracts.common import (
    BatchFileStatus,
    BatchImportSource,
    ContributionLevelLabel,
    DifficultyLevel,
    ErrorCode,
    ErrorResponse,
    IngestionRunSourceType,
    PreferredLanguage,
    ReservationState,
)
from app.contracts.policy import CONTRACT_VERSION
from app.contracts.schemas import (
    AdminConfigResponse,
    AdminConfigUpdateRequest,
    AdminNetworkEdgesGeoJsonResponse,
    AdminNetworkGeoJsonResponse,
    AdminNetworkNodesGeoJsonResponse,
    AdminUsersResponse,
    AuthMeResponse,
    BatchImportStartRequest,
    BatchImportStatusResponse,
    EdgeRatingPutRequest,
    EdgeRatingResponse,
    EntitlementResponse,
    HealthResponse,
    LocationSearchResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    MyUploadsResponse,
    NetworkEdgesViewportResponse,
    PublicConfigResponse,
    RegisterRequest,
    RegisterResponse,
    RouteCreateRequest,
    RouteCreateResponse,
    UploadConfirmRequest,
    UploadConfirmResponse,
    UploadDeleteResponse,
    UploadValidateResponse,
    UserPreferencesPatchRequest,
    UserPreferencesResponse,
)

_ERROR_DESCRIPTIONS: dict[int, str] = {
    401: "Authentication required or session invalid.",
    403: "Authenticated caller is not authorized or not entitled.",
    404: "Requested resource was not found.",
    409: "Conflict with current resource or idempotency state.",
    422: "Request validation failed.",
    429: "Rate limit or quota exceeded.",
    503: "Dependent service temporarily unavailable.",
}

_MULTIPART_VALIDATE_REQUEST_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "Untrusted GPX track file payload.",
                        }
                    },
                }
            }
        },
    }
}


def _error_responses(
    *status_codes: int,
    extra: dict[int | str, dict[str, Any]] | None = None,
) -> dict[int | str, dict[str, Any]]:
    """Build typed ErrorResponse OpenAPI entries for the given HTTP status codes."""
    responses: dict[int | str, dict[str, Any]] = {}
    for code in status_codes:
        responses[code] = {
            "model": ErrorResponse,
            "description": _ERROR_DESCRIPTIONS[code],
        }
    if extra:
        responses.update(extra)
    return responses


def _unimplemented_stub() -> NoReturn:
    raise NotImplementedError("Contract-only OpenAPI route stub is not wired for runtime traffic.")


def create_contract_openapi_app() -> FastAPI:
    """Create a pure schema-only FastAPI app registering all 23 ARCH §8 HTTP operations."""
    app = FastAPI(
        title="OnTrack API",
        version=CONTRACT_VERSION,
        description=(
            "Frozen T-009 HTTP API contract for OnTrack v2 covering all 20 "
            "ARCHITECTURE.md §8 rows (23 HTTP operations)."
        ),
    )

    # 1. POST /api/auth/register
    @app.post(
        "/api/auth/register",
        operation_id="authRegister",
        tags=["auth"],
        status_code=status.HTTP_201_CREATED,
        response_model=RegisterResponse,
        responses=_error_responses(409, 422, 429),
        summary="Register a new user account",
    )
    def auth_register(payload: RegisterRequest) -> RegisterResponse:
        del payload
        return _unimplemented_stub()

    # 2. POST /api/auth/login
    @app.post(
        "/api/auth/login",
        operation_id="authLogin",
        tags=["auth"],
        status_code=status.HTTP_200_OK,
        response_model=LoginResponse,
        responses=_error_responses(401, 403, 422, 429),
        summary="Authenticate user and establish session cookie",
    )
    def auth_login(payload: LoginRequest) -> LoginResponse:
        del payload
        return _unimplemented_stub()

    # 3. POST /api/auth/logout
    @app.post(
        "/api/auth/logout",
        operation_id="authLogout",
        tags=["auth"],
        status_code=status.HTTP_200_OK,
        response_model=LogoutResponse,
        responses=_error_responses(401, 422),
        summary="Revoke active session with optional multi-tab expected_session_id guard",
    )
    def auth_logout(payload: LogoutRequest | None = None) -> LogoutResponse:
        del payload
        return _unimplemented_stub()

    # 4. GET /api/auth/me
    @app.get(
        "/api/auth/me",
        operation_id="authMe",
        tags=["auth"],
        status_code=status.HTTP_200_OK,
        response_model=AuthMeResponse,
        responses=_error_responses(401, 403),
        summary="Fetch authenticated user profile, session_id, language, and contribution level",
    )
    def auth_me() -> AuthMeResponse:
        return _unimplemented_stub()

    # 5. PATCH /api/me/preferences
    @app.patch(
        "/api/me/preferences",
        operation_id="patchMyPreferences",
        tags=["me"],
        status_code=status.HTTP_200_OK,
        response_model=UserPreferencesResponse,
        responses=_error_responses(401, 403, 422),
        summary="Update authenticated user UI language preference",
    )
    def patch_my_preferences(payload: UserPreferencesPatchRequest) -> UserPreferencesResponse:
        del payload
        return _unimplemented_stub()

    # 6. GET /api/health
    @app.get(
        "/api/health",
        operation_id="getHealth",
        tags=["system"],
        status_code=status.HTTP_200_OK,
        response_model=HealthResponse,
        responses=_error_responses(503),
        summary="Check backend and database availability",
    )
    def get_health() -> HealthResponse:
        return _unimplemented_stub()

    # 7. GET /api/config/public
    @app.get(
        "/api/config/public",
        operation_id="getPublicConfig",
        tags=["system"],
        status_code=status.HTTP_200_OK,
        response_model=PublicConfigResponse,
        summary="Fetch non-secret public client configuration",
    )
    def get_public_config() -> PublicConfigResponse:
        return _unimplemented_stub()

    # 8. GET /api/map/search
    @app.get(
        "/api/map/search",
        operation_id="searchMapLocations",
        tags=["map"],
        status_code=status.HTTP_200_OK,
        response_model=LocationSearchResponse,
        responses=_error_responses(401, 403, 422, 429, 503),
        summary="Search Hebrew or English map locations via provider-independent geocoding",
    )
    def search_map_locations(
        q: Annotated[str, Query(min_length=1, max_length=200, description="Location query string")],
        lang: Annotated[
            PreferredLanguage | None,
            Query(description="Optional preferred result language ('he' or 'en')"),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=20, description="Maximum number of search results"),
        ] = 5,
    ) -> LocationSearchResponse:
        del q, lang, limit
        return _unimplemented_stub()

    # 9. GET /api/me/entitlement
    @app.get(
        "/api/me/entitlement",
        operation_id="getMyEntitlement",
        tags=["me"],
        status_code=status.HTTP_200_OK,
        response_model=EntitlementResponse,
        responses=_error_responses(401, 403),
        summary="Fetch route-creation entitlement, grace, allowance, and contribution level",
    )
    def get_my_entitlement() -> EntitlementResponse:
        return _unimplemented_stub()

    # 10. POST /api/routes
    @app.post(
        "/api/routes",
        operation_id="createRoute",
        tags=["routes"],
        status_code=status.HTTP_200_OK,
        response_model=RouteCreateResponse,
        responses=_error_responses(401, 403, 409, 422, 429, 503),
        summary="Calculate a continuity-verified hybrid off-road route",
    )
    def create_route(payload: RouteCreateRequest) -> RouteCreateResponse:
        del payload
        return _unimplemented_stub()

    # 11. POST /api/uploads/validate
    @app.post(
        "/api/uploads/validate",
        operation_id="validateUpload",
        tags=["uploads"],
        status_code=status.HTTP_200_OK,
        response_model=UploadValidateResponse,
        responses=_error_responses(401, 403, 409, 422, 429, 503),
        openapi_extra=_MULTIPART_VALIDATE_REQUEST_BODY,
        summary="Validate a GPX track upload, durably persist it, and create a DRAFT",
    )
    def validate_upload() -> UploadValidateResponse:
        return _unimplemented_stub()

    # 12. POST /api/uploads/{id}/confirm
    @app.post(
        "/api/uploads/{id}/confirm",
        operation_id="confirmUpload",
        tags=["uploads"],
        status_code=status.HTTP_202_ACCEPTED,
        response_model=UploadConfirmResponse,
        responses=_error_responses(401, 403, 404, 409, 422, 429),
        summary="Confirm a validated DRAFT upload with overall difficulty and Idempotency-Key",
    )
    def confirm_upload(
        id: Annotated[UUID, Path(description="Draft upload UUID")],
        payload: UploadConfirmRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
                description="Client-generated idempotency key bound to payload fingerprint",
            ),
        ],
    ) -> UploadConfirmResponse:
        del id, payload, idempotency_key
        return _unimplemented_stub()

    # 13. DELETE /api/uploads/{id}
    @app.delete(
        "/api/uploads/{id}",
        operation_id="deleteUploadDraft",
        tags=["uploads"],
        status_code=status.HTTP_200_OK,
        response_model=UploadDeleteResponse,
        responses=_error_responses(401, 403, 404, 409, 422),
        summary="Cancel and delete a DRAFT upload",
    )
    def delete_upload_draft(
        id: Annotated[UUID, Path(description="Draft upload UUID")],
    ) -> UploadDeleteResponse:
        del id
        return _unimplemented_stub()

    # 14. GET /api/me/uploads
    @app.get(
        "/api/me/uploads",
        operation_id="getMyUploads",
        tags=["me"],
        status_code=status.HTTP_200_OK,
        response_model=MyUploadsResponse,
        responses=_error_responses(401, 403),
        summary="List authenticated user's My Tracks submissions with entitlement and level",
    )
    def get_my_uploads() -> MyUploadsResponse:
        return _unimplemented_stub()

    # 15. GET /api/admin/config
    @app.get(
        "/api/admin/config",
        operation_id="getAdminConfig",
        tags=["admin"],
        status_code=status.HTTP_200_OK,
        response_model=AdminConfigResponse,
        responses=_error_responses(401, 403),
        summary="Fetch editable FR-022 application settings (system role only)",
    )
    def get_admin_config() -> AdminConfigResponse:
        return _unimplemented_stub()

    # 16. PUT /api/admin/config
    @app.put(
        "/api/admin/config",
        operation_id="updateAdminConfig",
        tags=["admin"],
        status_code=status.HTTP_200_OK,
        response_model=AdminConfigResponse,
        responses=_error_responses(401, 403, 422),
        summary="Atomically update FR-022 application settings (system role only)",
    )
    def update_admin_config(payload: AdminConfigUpdateRequest) -> AdminConfigResponse:
        del payload
        return _unimplemented_stub()

    # 17. GET /api/admin/users
    @app.get(
        "/api/admin/users",
        operation_id="getAdminUsers",
        tags=["admin"],
        status_code=status.HTTP_200_OK,
        response_model=AdminUsersResponse,
        responses=_error_responses(401, 403),
        summary="List read-only user directory (system role only)",
    )
    def get_admin_users() -> AdminUsersResponse:
        return _unimplemented_stub()

    # 18. GET /api/admin/network/edges
    @app.get(
        "/api/admin/network/edges",
        operation_id="getAdminNetworkEdges",
        tags=["admin"],
        status_code=status.HTTP_200_OK,
        response_model=AdminNetworkEdgesGeoJsonResponse,
        responses=_error_responses(
            401,
            403,
            422,
            extra={304: {"description": "Not Modified (matching single-snapshot ETag)"}},
        ),
        summary="Fetch all ACTIVE trail network edges as GeoJSON with single-snapshot ETag",
    )
    def get_admin_network_edges(
        if_none_match: Annotated[
            str | None,
            Header(
                alias="If-None-Match",
                description="Optional ETag for conditional 304 validation (auth checked first)",
            ),
        ] = None,
    ) -> AdminNetworkEdgesGeoJsonResponse:
        del if_none_match
        return _unimplemented_stub()

    # 19. GET /api/admin/network/nodes
    @app.get(
        "/api/admin/network/nodes",
        operation_id="getAdminNetworkNodes",
        tags=["admin"],
        status_code=status.HTTP_200_OK,
        response_model=AdminNetworkNodesGeoJsonResponse,
        responses=_error_responses(
            401,
            403,
            422,
            extra={304: {"description": "Not Modified (matching single-snapshot ETag)"}},
        ),
        summary="Fetch all ACTIVE trail network nodes as GeoJSON with single-snapshot ETag",
    )
    def get_admin_network_nodes(
        if_none_match: Annotated[
            str | None,
            Header(
                alias="If-None-Match",
                description="Optional ETag for conditional 304 validation (auth checked first)",
            ),
        ] = None,
    ) -> AdminNetworkNodesGeoJsonResponse:
        del if_none_match
        return _unimplemented_stub()

    # 20. GET /api/network/edges
    @app.get(
        "/api/network/edges",
        operation_id="getNetworkEdges",
        tags=["network"],
        status_code=status.HTTP_200_OK,
        response_model=NetworkEdgesViewportResponse,
        responses=_error_responses(401, 403, 422, 429),
        summary="Fetch ACTIVE selectable network edges inside a validated viewport",
    )
    def get_network_edges(
        bbox: Annotated[
            str,
            Query(
                description="Viewport bounding box formatted as min_lng,min_lat,max_lng,max_lat",
            ),
        ],
        zoom: Annotated[int, Query(ge=1, le=22, description="Current map zoom level")],
    ) -> NetworkEdgesViewportResponse:
        del bbox, zoom
        return _unimplemented_stub()

    # 21. GET /api/network/edges/{id}/rating
    @app.get(
        "/api/network/edges/{id}/rating",
        operation_id="getEdgeRating",
        tags=["network"],
        status_code=status.HTTP_200_OK,
        response_model=EdgeRatingResponse,
        responses=_error_responses(401, 403, 404, 422),
        summary="Fetch effective edge difficulty, report count, and caller's own rating",
    )
    def get_edge_rating(
        id: Annotated[int, Path(ge=1, description="Stable ACTIVE trail network edge ID")],
    ) -> EdgeRatingResponse:
        del id
        return _unimplemented_stub()

    # 22. PUT /api/network/edges/{id}/rating
    @app.put(
        "/api/network/edges/{id}/rating",
        operation_id="putEdgeRating",
        tags=["network"],
        status_code=status.HTTP_200_OK,
        response_model=EdgeRatingResponse,
        responses=_error_responses(401, 403, 404, 422, 429),
        summary="Upsert caller's explicit difficulty rating (1..3) for an ACTIVE edge",
    )
    def put_edge_rating(
        id: Annotated[int, Path(ge=1, description="Stable ACTIVE trail network edge ID")],
        payload: EdgeRatingPutRequest,
    ) -> EdgeRatingResponse:
        del id, payload
        return _unimplemented_stub()

    # 23. POST /api/admin/imports
    @app.post(
        "/api/admin/imports",
        operation_id="startAdminBatchImport",
        tags=["admin"],
        status_code=status.HTTP_202_ACCEPTED,
        response_model=BatchImportStatusResponse,
        responses=_error_responses(401, 403, 409, 422),
        summary="Start an asynchronous administrator batch track import (system role only)",
    )
    def start_admin_batch_import(
        payload: BatchImportStartRequest,
    ) -> BatchImportStatusResponse:
        del payload
        return _unimplemented_stub()

    return app


def _collect_refs(node: Any, refs: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.removeprefix("#/components/schemas/"))
        for value in node.values():
            _collect_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, refs)


def _register_extra_component_schemas(schemas: dict[str, Any]) -> None:
    """Ensure shared frozen contract enums/aliases are exported into OpenAPI components."""
    extra_types: list[tuple[str, Any]] = [
        ("ErrorCode", ErrorCode),
        ("BatchFileStatus", BatchFileStatus),
        ("ReservationState", ReservationState),
        ("PreferredLanguage", PreferredLanguage),
        ("DifficultyLevel", DifficultyLevel),
        ("BatchImportSource", BatchImportSource),
        ("ContributionLevelLabel", ContributionLevelLabel),
        ("IngestionRunSourceType", IngestionRunSourceType),
        ("AdminNetworkGeoJsonResponse", AdminNetworkGeoJsonResponse),
    ]
    for name, schema_type in extra_types:
        if name in schemas:
            continue
        generated = TypeAdapter(schema_type).json_schema(
            ref_template="#/components/schemas/{model}"
        )
        defs = generated.pop("$defs", None)
        if isinstance(defs, dict):
            for def_name, def_schema in defs.items():
                schemas.setdefault(def_name, def_schema)
        generated.setdefault("title", name)
        schemas[name] = generated


def export_openapi_schema() -> dict[str, Any]:
    """Return a clean, deterministic OpenAPI 3.1 document for the frozen T-009 contract."""
    app = create_contract_openapi_app()
    schema = copy.deepcopy(app.openapi())
    components = schema.setdefault("components", {})
    schemas: dict[str, Any] = components.setdefault("schemas", {})

    # Remove unused default FastAPI validation models if every route declares ErrorResponse.
    referenced_by_paths: set[str] = set()
    _collect_refs(schema.get("paths", {}), referenced_by_paths)
    for unused_default in ("HTTPValidationError", "ValidationError"):
        if unused_default not in referenced_by_paths:
            schemas.pop(unused_default, None)

    _register_extra_component_schemas(schemas)
    return schema


__all__ = [
    "create_contract_openapi_app",
    "export_openapi_schema",
]
