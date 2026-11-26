import type {
  AdminConfigResponse,
  AdminConfigUpdateRequest,
  AdminNetworkEdgesGeoJsonResponse,
  AdminNetworkNodesGeoJsonResponse,
  AdminUsersResponse,
  AuthMeResponse,
  BatchImportStartRequest,
  BatchImportStatusResponse,
  ContributionLevelInfo,
  EdgeRatingPutRequest,
  EdgeRatingResponse,
  EntitlementResponse,
  ErrorResponse,
  HealthResponse,
  IngestionRunSummarySchema,
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
} from '../../api-client/models';

const FIXED_TIMESTAMP = '2026-11-26T10:00:00Z';
const FIXED_EXPIRES_AT = '2026-11-26T11:00:00Z';
const MOCK_SESSION_ID = '11111111-1111-4111-8111-111111111111';
const MOCK_REQUEST_ID = '22222222-2222-4222-8222-222222222222';
const MOCK_UPLOAD_ID = '33333333-3333-4333-8333-333333333333';
const MOCK_RUN_ID = '44444444-4444-4444-8444-444444444444';
const MOCK_BATCH_ID = '55555555-5555-4555-8555-555555555555';

export const MOCK_CONTRIBUTION_LEVEL_ADVENTURER: Readonly<ContributionLevelInfo> = {
  level: 'Adventurer',
  badge_key: 'adventurer',
  active_count: 6,
  next_level: 'Trailblazer',
  tracks_to_next: 4,
};

export const MOCK_REGISTER_REQUEST: Readonly<RegisterRequest> = {
  first_name: 'Eyal',
  last_name: 'Cohen',
  email: 'eyal@example.com',
  username: 'EyalRider',
  password: 'Pass1!',
  preferred_language: 'he',
};

export const MOCK_REGISTER_RESPONSE: Readonly<RegisterResponse> = {
  status: 'succeed',
  user_id: 101,
  username: 'EyalRider',
  user_type: 'regular',
  preferred_language: 'he',
};

export const MOCK_LOGIN_REQUEST: Readonly<LoginRequest> = {
  username: 'EyalRider',
  password: 'Pass1!',
};

export const MOCK_LOGIN_RESPONSE: Readonly<LoginResponse> = {
  user: {
    id: 101,
    username: 'EyalRider',
    first_name: 'Eyal',
    last_name: 'Cohen',
    email: 'eyal@example.com',
    user_type: 'regular',
    preferred_language: 'he',
  },
  session_id: MOCK_SESSION_ID,
  preferred_language: 'he',
  contribution_level: MOCK_CONTRIBUTION_LEVEL_ADVENTURER,
};

export const MOCK_LOGOUT_REQUEST: Readonly<LogoutRequest> = {
  expected_session_id: MOCK_SESSION_ID,
};

export const MOCK_LOGOUT_RESPONSE: Readonly<LogoutResponse> = {
  revoked: true,
  reason: 'revoked',
};

export const MOCK_AUTH_ME_RESPONSE: Readonly<AuthMeResponse> = {
  id: 101,
  username: 'EyalRider',
  first_name: 'Eyal',
  last_name: 'Cohen',
  email: 'eyal@example.com',
  user_type: 'regular',
  preferred_language: 'he',
  session_id: MOCK_SESSION_ID,
  contribution_level: MOCK_CONTRIBUTION_LEVEL_ADVENTURER,
};

export const MOCK_USER_PREFERENCES_PATCH_REQUEST: Readonly<UserPreferencesPatchRequest> = {
  preferred_language: 'en',
};

export const MOCK_USER_PREFERENCES_RESPONSE: Readonly<UserPreferencesResponse> = {
  preferred_language: 'en',
};

export const MOCK_HEALTH_RESPONSE: Readonly<HealthResponse> = {
  status: 'ok',
  database: 'ok',
};

export const MOCK_PUBLIC_CONFIG_RESPONSE: Readonly<PublicConfigResponse> = {
  support_email: 'support@ontrack.local',
  network_layer_min_zoom: 12,
  max_upload_mb: 10,
};

export const MOCK_LOCATION_SEARCH_RESPONSE_HE: Readonly<LocationSearchResponse> = {
  query: 'חיפה',
  results: [
    {
      place_id: 'il-haifa-1',
      display_name: 'חיפה, ישראל',
      lat: 32.794,
      lng: 34.9896,
      category: 'city',
      address: 'מחוז חיפה, ישראל',
    },
    {
      place_id: 'il-carmel-2',
      display_name: 'פארק הכרמל, ישראל',
      lat: 32.731,
      lng: 35.018,
      category: 'park',
      address: 'הר הכרמל, ישראל',
    },
  ],
};

export const MOCK_LOCATION_SEARCH_RESPONSE_EN: Readonly<LocationSearchResponse> = {
  query: 'Mount Carmel',
  results: [
    {
      place_id: 'il-carmel-2',
      display_name: 'Mount Carmel National Park, Israel',
      lat: 32.731,
      lng: 35.018,
      category: 'park',
      address: 'Haifa District, Israel',
    },
  ],
};

export const MOCK_ENTITLEMENT_ALLOWED_RESPONSE: Readonly<EntitlementResponse> = {
  allowed: true,
  reason: null,
  admin_exempt: false,
  contribution_eligible: true,
  qualifying_count: 6,
  min_required_tracks: 1,
  free_allowance_limit: 0,
  free_allowance_remaining: 0,
  grace_until: null,
  contribution_level: MOCK_CONTRIBUTION_LEVEL_ADVENTURER,
};

export const MOCK_ENTITLEMENT_DENIED_RESPONSE: Readonly<EntitlementResponse> = {
  allowed: false,
  reason: 'BELOW_THRESHOLD',
  admin_exempt: false,
  contribution_eligible: false,
  qualifying_count: 0,
  min_required_tracks: 1,
  free_allowance_limit: 0,
  free_allowance_remaining: 0,
  grace_until: null,
  contribution_level: {
    level: 'Explorer',
    badge_key: 'explorer',
    active_count: 0,
    next_level: 'Adventurer',
    tracks_to_next: 6,
  },
};

export const MOCK_ROUTE_CREATE_REQUEST: Readonly<RouteCreateRequest> = {
  request_id: MOCK_REQUEST_ID,
  start_lat: 32.731,
  start_lng: 35.018,
  end_lat: 32.685,
  end_lng: 35.062,
  include_diagnostics: false,
};

export const MOCK_ROUTE_CREATE_RESPONSE: Readonly<RouteCreateResponse> = {
  request_id: MOCK_REQUEST_ID,
  replayed: false,
  diagnostics: null,
  segments: [
    {
      source: 'external',
      provider_profile: 'driving-car',
      edge_id: null,
      avg_difficulty: null,
      display_level: null,
      points: [
        { lat: 32.731, lng: 35.018, ele: 310.0 },
        { lat: 32.725, lng: 35.024, ele: 295.0 },
      ],
    },
    {
      source: 'network',
      edge_id: 42,
      provider_profile: null,
      avg_difficulty: 2.25,
      display_level: 2,
      points: [
        { lat: 32.725, lng: 35.024, ele: 295.0 },
        { lat: 32.695, lng: 35.051, ele: 240.0 },
      ],
    },
    {
      source: 'external',
      provider_profile: 'driving-car',
      edge_id: null,
      avg_difficulty: null,
      display_level: null,
      points: [
        { lat: 32.695, lng: 35.051, ele: 240.0 },
        { lat: 32.685, lng: 35.062, ele: 215.0 },
      ],
    },
  ],
};

export const MOCK_UPLOAD_VALIDATE_RESPONSE: Readonly<UploadValidateResponse> = {
  upload_id: MOCK_UPLOAD_ID,
  filename: 'nahal-oren-carmel.gpx',
  status: 'DRAFT',
  credit_state: 'NONE',
  point_count: 3,
  segments: [
    {
      segment_index: 0,
      points: [
        { lat: 32.725, lng: 35.024, ele: 295.0 },
        { lat: 32.712, lng: 35.035, ele: 270.0 },
        { lat: 32.695, lng: 35.051, ele: 240.0 },
      ],
    },
  ],
  expires_at: FIXED_EXPIRES_AT,
};

export const MOCK_UPLOAD_CONFIRM_REQUEST: Readonly<UploadConfirmRequest> = {
  overall_difficulty: 2,
};

export const MOCK_UPLOAD_CONFIRM_RESPONSE: Readonly<UploadConfirmResponse> = {
  upload_id: MOCK_UPLOAD_ID,
  filename: 'nahal-oren-carmel.gpx',
  status: 'PENDING',
  credit_state: 'PROVISIONAL',
  overall_difficulty: 2,
  accepted_at: FIXED_TIMESTAMP,
  entitlement: MOCK_ENTITLEMENT_ALLOWED_RESPONSE,
};

export const MOCK_UPLOAD_DELETE_RESPONSE: Readonly<UploadDeleteResponse> = {
  upload_id: MOCK_UPLOAD_ID,
  deleted: true,
};

export const MOCK_INGESTION_RUN_SUMMARY: Readonly<IngestionRunSummarySchema> = {
  id: MOCK_RUN_ID,
  source_type: 'upload',
  status: 'COMPLETED',
  track_id: 'sha256:normalized-carmel-track',
  graph_revision_before: 11,
  created_at: FIXED_TIMESTAMP,
  completed_at: FIXED_TIMESTAMP,
};

export const MOCK_MY_UPLOADS_RESPONSE: Readonly<MyUploadsResponse> = {
  uploads: [
    {
      upload_id: MOCK_UPLOAD_ID,
      filename: 'nahal-oren-carmel.gpx',
      status: 'ACTIVE',
      credit_state: 'PERMANENT',
      overall_difficulty: 2,
      rejection_code: null,
      safe_error_message: null,
      submitted_at: FIXED_TIMESTAMP,
      processed_at: FIXED_TIMESTAMP,
      latest_ingestion_run: MOCK_INGESTION_RUN_SUMMARY,
    },
  ],
  entitlement: MOCK_ENTITLEMENT_ALLOWED_RESPONSE,
  contribution_level: MOCK_CONTRIBUTION_LEVEL_ADVENTURER,
};

export const MOCK_NETWORK_EDGES_VIEWPORT_RESPONSE: Readonly<NetworkEdgesViewportResponse> = {
  edges: [
    {
      edge_id: 42,
      points: [
        { lat: 32.725, lng: 35.024, ele: 295.0 },
        { lat: 32.695, lng: 35.051, ele: 240.0 },
      ],
      length_m: 4180.5,
      avg_difficulty: 2.25,
      display_level: 2,
      report_count: 4,
    },
  ],
  truncated: false,
  graph_revision: 12,
};

export const MOCK_EDGE_RATING_PUT_REQUEST: Readonly<EdgeRatingPutRequest> = {
  difficulty: 3,
};

export const MOCK_EDGE_RATING_RESPONSE: Readonly<EdgeRatingResponse> = {
  edge_id: 42,
  avg_difficulty: 2.4,
  display_level: 2,
  report_count: 5,
  user_rating: 3,
  graph_revision: 13,
};

export const MOCK_ADMIN_CONFIG_RESPONSE: Readonly<AdminConfigResponse> = {
  min_required_tracks: 1,
  point_simplification_distance_m: 20.0,
  node_snap_radius_m: 20.0,
  free_route_creations: 0,
  rejected_track_grace_period_hours: 24.0,
  updated_at: FIXED_TIMESTAMP,
  updated_by: 1,
};

export const MOCK_ADMIN_CONFIG_UPDATE_REQUEST: Readonly<AdminConfigUpdateRequest> = {
  min_required_tracks: 1,
  point_simplification_distance_m: 20.0,
  node_snap_radius_m: 10.0,
  free_route_creations: 2,
  rejected_track_grace_period_hours: 24.0,
};

export const MOCK_ADMIN_USERS_RESPONSE: Readonly<AdminUsersResponse> = {
  users: [
    {
      id: 1,
      username: 'SystemAdmin',
      first_name: 'Admin',
      last_name: 'User',
      email: 'admin@example.com',
      user_type: 'system',
    },
    {
      id: 101,
      username: 'EyalRider',
      first_name: 'Eyal',
      last_name: 'Cohen',
      email: 'eyal@example.com',
      user_type: 'regular',
    },
  ],
};

export const MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE: Readonly<AdminNetworkEdgesGeoJsonResponse> =
  {
    type: 'FeatureCollection',
    graph_revision: 13,
    features: [
      {
        type: 'Feature',
        id: 42,
        geometry: {
          type: 'LineString',
          coordinates: [
            [35.024, 32.725],
            [35.051, 32.695],
          ],
        },
        properties: {
          edge_id: 42,
          length_m: 4180.5,
          avg_difficulty: 2.4,
          display_level: 2,
          report_count: 5,
        },
      },
    ],
  };

export const MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE: Readonly<AdminNetworkNodesGeoJsonResponse> =
  {
    type: 'FeatureCollection',
    graph_revision: 13,
    features: [
      {
        type: 'Feature',
        id: 10,
        geometry: {
          type: 'Point',
          coordinates: [35.024, 32.725, 295.0],
        },
        properties: {
          node_id: 10,
          ele: 295.0,
        },
      },
    ],
  };

export const MOCK_BATCH_IMPORT_START_REQUEST: Readonly<BatchImportStartRequest> = {
  source: 'server',
};

export const MOCK_BATCH_IMPORT_STATUS_RESPONSE: Readonly<BatchImportStatusResponse> = {
  batch_id: MOCK_BATCH_ID,
  source: 'server',
  status: 'QUEUED',
  total_files: 15,
  imported_count: 0,
  skipped_count: 0,
  failed_count: 0,
  started_at: FIXED_TIMESTAMP,
  finished_at: null,
};

export const MOCK_ERROR_RESPONSE: Readonly<ErrorResponse> = {
  code: 'ROUTE_NOT_ALLOWED',
  message: 'Upload at least 1 track to unlock route planning.',
  details: {
    reason: 'BELOW_THRESHOLD',
  },
};
