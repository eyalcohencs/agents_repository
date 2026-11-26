// AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.
// Regenerate via: cd backend && .venv/bin/python -m app.contracts.codegen

export interface AdminConfigResponse {
  free_route_creations: number;
  min_required_tracks: number;
  node_snap_radius_m: number;
  point_simplification_distance_m: number;
  rejected_track_grace_period_hours: number;
  updated_at?: string | null;
  updated_by?: number | null;
}

export interface AdminConfigUpdateRequest {
  free_route_creations: number;
  min_required_tracks: number;
  node_snap_radius_m: number;
  point_simplification_distance_m: number;
  rejected_track_grace_period_hours: number;
}

export interface AdminNetworkEdgeFeature {
  geometry: GeoJsonLineStringGeometry;
  id: number;
  properties: AdminNetworkEdgeProperties;
  type?: 'Feature';
}

export interface AdminNetworkEdgeProperties {
  avg_difficulty: number;
  display_level: 1 | 2 | 3;
  edge_id: number;
  length_m: number;
  report_count: number;
}

export interface AdminNetworkEdgesGeoJsonResponse {
  features: AdminNetworkEdgeFeature[];
  graph_revision: number;
  type?: 'FeatureCollection';
}

export interface AdminNetworkGeoJsonResponse {
  features: (AdminNetworkEdgeFeature | AdminNetworkNodeFeature)[];
  graph_revision: number;
  type?: 'FeatureCollection';
}

export interface AdminNetworkNodeFeature {
  geometry: GeoJsonPointGeometry;
  id: number;
  properties: AdminNetworkNodeProperties;
  type?: 'Feature';
}

export interface AdminNetworkNodeProperties {
  ele?: number | null;
  node_id: number;
}

export interface AdminNetworkNodesGeoJsonResponse {
  features: AdminNetworkNodeFeature[];
  graph_revision: number;
  type?: 'FeatureCollection';
}

export interface AdminUserSchema {
  email: string;
  first_name: string;
  id: number;
  last_name: string;
  user_type: UserRole;
  username: string;
}

export interface AdminUsersResponse {
  users: AdminUserSchema[];
}

export interface AuthMeResponse {
  contribution_level: ContributionLevelInfo;
  email: string;
  first_name: string;
  id: number;
  last_name: string;
  preferred_language?: 'he' | 'en';
  session_id: string;
  user_type: UserRole;
  username: string;
}

export interface AuthenticatedUserSchema {
  email: string;
  first_name: string;
  id: number;
  last_name: string;
  preferred_language?: 'he' | 'en';
  user_type: UserRole;
  username: string;
}

export type BatchFileStatus = 'PENDING' | 'PROCESSING' | 'RETRY' | 'COMPLETED' | 'SKIPPED' | 'FAILED';

export type BatchImportSource = 'cloud' | 'server';

export interface BatchImportStartRequest {
  source: 'cloud' | 'server';
}

export type BatchImportStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'COMPLETED_WITH_ERRORS' | 'FAILED';

export interface BatchImportStatusResponse {
  batch_id: string;
  failed_count: number;
  finished_at?: string | null;
  imported_count: number;
  skipped_count: number;
  source: 'cloud' | 'server';
  started_at: string;
  status: BatchImportStatus;
  total_files: number;
}

export interface ContributionLevelInfo {
  active_count: number;
  badge_key: ContributionLevelKey;
  level: 'Explorer' | 'Adventurer' | 'Trailblazer' | 'Trail Master';
  next_level?: 'Explorer' | 'Adventurer' | 'Trailblazer' | 'Trail Master' | null;
  tracks_to_next?: number | null;
}

export type ContributionLevelKey = 'explorer' | 'adventurer' | 'trailblazer' | 'trail_master';

export type ContributionLevelLabel = 'Explorer' | 'Adventurer' | 'Trailblazer' | 'Trail Master';

export interface CoordinateSchema {
  ele?: number | null;
  lat: number;
  lng: number;
}

export type CreditState = 'NONE' | 'PROVISIONAL' | 'PERMANENT';

export type DifficultyLevel = 1 | 2 | 3;

export interface EdgeRatingPutRequest {
  difficulty: 1 | 2 | 3;
}

export interface EdgeRatingResponse {
  avg_difficulty: number;
  display_level: 1 | 2 | 3;
  edge_id: number;
  graph_revision: number;
  report_count: number;
  user_rating?: 1 | 2 | 3 | null;
}

export type EntitlementDenialReason = 'BELOW_THRESHOLD' | 'ALLOWANCE_EXHAUSTED' | 'ACCOUNT_INACTIVE';

export interface EntitlementResponse {
  admin_exempt?: boolean;
  allowed: boolean;
  contribution_eligible: boolean;
  contribution_level: ContributionLevelInfo;
  free_allowance_limit: number;
  free_allowance_remaining: number;
  grace_until?: string | null;
  min_required_tracks: number;
  qualifying_count: number;
  reason?: EntitlementDenialReason | null;
}

export type ErrorCode = 'BAD_REQUEST' | 'UNAUTHORIZED' | 'FORBIDDEN' | 'NOT_FOUND' | 'METHOD_NOT_ALLOWED' | 'CONFLICT' | 'VALIDATION_ERROR' | 'RATE_LIMITED' | 'INTERNAL_ERROR' | 'SERVICE_UNAVAILABLE' | 'INVALID_CREDENTIALS' | 'USERNAME_TAKEN' | 'EMAIL_TAKEN' | 'SESSION_EXPIRED' | 'SESSION_REVOKED' | 'ACCOUNT_INACTIVE' | 'CSRF_VALIDATION_FAILED' | 'ROUTE_NOT_ALLOWED' | 'RESERVATION_EXPIRED' | 'REQUEST_ID_EXPIRED' | 'REQUEST_ID_CONFLICT' | 'REQUEST_IN_FLIGHT' | 'IDEMPOTENCY_KEY_MISMATCH' | 'EMPTY_FILE' | 'UNSUPPORTED_FORMAT' | 'MALFORMED_XML' | 'UNSAFE_XML' | 'INVALID_COORDINATE' | 'DEGENERATE_GEOMETRY' | 'FILE_TOO_LARGE' | 'TOO_MANY_POINTS' | 'DRAFT_LIMIT_EXCEEDED' | 'DUPLICATE_UPLOAD' | 'STORAGE_FAILURE' | 'ROUTING_PROVIDER_ERROR' | 'ROUTING_PROVIDER_QUOTA' | 'ROUTE_CONTINUITY_FAILED' | 'GEOCODING_PROVIDER_ERROR' | 'GEOCODING_PROVIDER_QUOTA' | 'VIEWPORT_TOO_LARGE' | 'BATCH_IMPORT_ALREADY_RUNNING';

export interface ErrorResponse {
  code: string;
  details?: Record<string, unknown> | unknown[] | null;
  message: string;
}

export interface GeoJsonLineStringGeometry {
  coordinates: [number, number][];
  type?: 'LineString';
}

export interface GeoJsonPointGeometry {
  coordinates: [number, number] | [number, number, number];
  type?: 'Point';
}

export interface HealthResponse {
  database: 'ok';
  status: 'ok';
}

export type IngestionRunSourceType = 'upload' | 'batch_file';

export type IngestionRunStatus = 'RUNNING' | 'COMPLETED' | 'REJECTED' | 'FAILED';

export interface IngestionRunSummarySchema {
  completed_at?: string | null;
  created_at: string;
  graph_revision_before: number;
  id: string;
  source_type: 'upload' | 'batch_file';
  status: IngestionRunStatus;
  track_id: string;
}

export interface LocationSearchResponse {
  query: string;
  results: LocationSearchResultSchema[];
}

export interface LocationSearchResultSchema {
  address?: string | null;
  category?: string | null;
  display_name: string;
  lat: number;
  lng: number;
  place_id: string;
}

export interface LoginRequest {
  password: string;
  username: string;
}

export interface LoginResponse {
  contribution_level: ContributionLevelInfo;
  preferred_language?: 'he' | 'en';
  session_id: string;
  user: AuthenticatedUserSchema;
}

export interface LogoutRequest {
  expected_session_id?: string | null;
}

export interface LogoutResponse {
  reason?: 'revoked' | 'superseded' | 'already_revoked';
  revoked: boolean;
}

export interface MyUploadItemSchema {
  credit_state: CreditState;
  filename: string;
  latest_ingestion_run?: IngestionRunSummarySchema | null;
  overall_difficulty?: 1 | 2 | 3 | null;
  processed_at?: string | null;
  rejection_code?: RejectionCode | null;
  safe_error_message?: string | null;
  status: UploadStatus;
  submitted_at: string;
  upload_id: string;
}

export interface MyUploadsResponse {
  contribution_level: ContributionLevelInfo;
  entitlement: EntitlementResponse;
  uploads: MyUploadItemSchema[];
}

export interface NetworkEdgeSchema {
  avg_difficulty: number;
  display_level: 1 | 2 | 3;
  edge_id: number;
  length_m: number;
  points: CoordinateSchema[];
  report_count: number;
}

export interface NetworkEdgesViewportResponse {
  edges: NetworkEdgeSchema[];
  graph_revision: number;
  truncated: boolean;
}

export type PreferredLanguage = 'he' | 'en';

export interface PublicConfigResponse {
  max_upload_mb: number;
  network_layer_min_zoom: number;
  support_email: string;
}

export interface RegisterRequest {
  email: string;
  first_name: string;
  last_name: string;
  password: string;
  preferred_language?: 'he' | 'en';
  username: string;
}

export interface RegisterResponse {
  preferred_language?: 'he' | 'en';
  status?: 'succeed';
  user_id: number;
  user_type?: 'regular';
  username: string;
}

export type RejectionCode = 'DEGENERATE_GEOMETRY' | 'OUTSIDE_SERVICE_AREA' | 'QUALITY_RULE_FAILED';

export type ReservationState = 'RESERVED' | 'CONSUMED' | 'RELEASED' | 'EXPIRED' | 'NON_ALLOWANCE';

export interface RouteCreateRequest {
  end_lat: number;
  end_lng: number;
  include_diagnostics?: boolean;
  request_id: string;
  start_lat: number;
  start_lng: number;
}

export interface RouteCreateResponse {
  diagnostics?: RouteDiagnosticsSchema | null;
  replayed?: boolean;
  request_id: string;
  segments: RouteSegmentSchema[];
}

export interface RouteDiagnosticsSchema {
  algorithm: 'pgr_dijkstra' | 'pgr_bdDijkstra';
  branch: 'connected' | 'entry_equals_exit' | 'disconnected' | 'no_entry_fallback' | 'entry_no_exit_fallback';
  cost_mode: 'distance' | 'hops';
  entry_distance_m?: number | null;
  entry_node_id?: number | null;
  exit_distance_m?: number | null;
  exit_node_id?: number | null;
  external_latency_ms: number;
  graph_revision?: number | null;
  internal_query_ms: number;
}

export interface RouteSegmentSchema {
  avg_difficulty?: number | null;
  display_level?: 1 | 2 | 3 | null;
  edge_id?: number | null;
  points: CoordinateSchema[];
  provider_profile?: string | null;
  source: 'network' | 'external';
}

export interface UploadConfirmRequest {
  overall_difficulty: 1 | 2 | 3;
}

export interface UploadConfirmResponse {
  accepted_at: string;
  credit_state: CreditState;
  entitlement: EntitlementResponse;
  filename: string;
  overall_difficulty: 1 | 2 | 3;
  status: UploadStatus;
  upload_id: string;
}

export interface UploadDeleteResponse {
  deleted?: true;
  upload_id: string;
}

export interface UploadPreviewSegmentSchema {
  points: CoordinateSchema[];
  segment_index: number;
}

export type UploadStatus = 'DRAFT' | 'PENDING' | 'PROCESSING' | 'RETRY' | 'TECHNICAL_FAILED' | 'ACTIVE' | 'REJECTED';

export interface UploadValidateResponse {
  credit_state?: 'NONE';
  expires_at: string;
  filename: string;
  point_count: number;
  segments: UploadPreviewSegmentSchema[];
  status?: 'DRAFT';
  upload_id: string;
}

export interface UserPreferencesPatchRequest {
  preferred_language: 'he' | 'en';
}

export interface UserPreferencesResponse {
  preferred_language: 'he' | 'en';
}

export type UserRole = 'regular' | 'system' | 'inactive';
