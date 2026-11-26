import { HttpHeaders, HttpResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  MOCK_ADMIN_CONFIG_RESPONSE,
  MOCK_ADMIN_CONFIG_UPDATE_REQUEST,
  MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE,
  MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE,
  MOCK_ADMIN_USERS_RESPONSE,
  MOCK_AUTH_ME_RESPONSE,
  MOCK_BATCH_IMPORT_START_REQUEST,
  MOCK_BATCH_IMPORT_STATUS_RESPONSE,
  MOCK_EDGE_RATING_PUT_REQUEST,
  MOCK_EDGE_RATING_RESPONSE,
  MOCK_ENTITLEMENT_ALLOWED_RESPONSE,
  MOCK_HEALTH_RESPONSE,
  MOCK_LOCATION_SEARCH_RESPONSE_HE,
  MOCK_LOGIN_REQUEST,
  MOCK_LOGIN_RESPONSE,
  MOCK_LOGOUT_REQUEST,
  MOCK_LOGOUT_RESPONSE,
  MOCK_MY_UPLOADS_RESPONSE,
  MOCK_NETWORK_EDGES_VIEWPORT_RESPONSE,
  MOCK_PUBLIC_CONFIG_RESPONSE,
  MOCK_REGISTER_REQUEST,
  MOCK_REGISTER_RESPONSE,
  MOCK_ROUTE_CREATE_REQUEST,
  MOCK_ROUTE_CREATE_RESPONSE,
  MOCK_UPLOAD_CONFIRM_REQUEST,
  MOCK_UPLOAD_CONFIRM_RESPONSE,
  MOCK_UPLOAD_DELETE_RESPONSE,
  MOCK_UPLOAD_VALIDATE_RESPONSE,
  MOCK_USER_PREFERENCES_PATCH_REQUEST,
  MOCK_USER_PREFERENCES_RESPONSE,
} from '../shared/contracts/mock-fixtures';
import { OnTrackApiClient } from './index';

describe('OnTrackApiClient (all 23 ARCH §8 operations)', () => {
  let client: OnTrackApiClient;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    client = TestBed.inject(OnTrackApiClient);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpTesting.verify();
  });

  it('1. POST /api/auth/register', () => {
    let result: unknown;
    client.authRegister(MOCK_REGISTER_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/auth/register');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_REGISTER_REQUEST);
    req.flush(MOCK_REGISTER_RESPONSE, { status: 201, statusText: 'Created' });
    expect(result).toEqual(MOCK_REGISTER_RESPONSE);
  });

  it('2. POST /api/auth/login', () => {
    let result: unknown;
    client.authLogin(MOCK_LOGIN_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/auth/login');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_LOGIN_REQUEST);
    req.flush(MOCK_LOGIN_RESPONSE);
    expect(result).toEqual(MOCK_LOGIN_RESPONSE);
  });

  it('3. POST /api/auth/logout (with and without expected_session_id)', () => {
    let resultWithBody: unknown;
    client.authLogout(MOCK_LOGOUT_REQUEST).subscribe((res) => (resultWithBody = res));

    const req1 = httpTesting.expectOne('/api/auth/logout');
    expect(req1.request.method).toBe('POST');
    expect(req1.request.withCredentials).toBe(true);
    expect(req1.request.body).toEqual(MOCK_LOGOUT_REQUEST);
    req1.flush(MOCK_LOGOUT_RESPONSE);
    expect(resultWithBody).toEqual(MOCK_LOGOUT_RESPONSE);

    let resultDefault: unknown;
    client.authLogout().subscribe((res) => (resultDefault = res));
    const req2 = httpTesting.expectOne('/api/auth/logout');
    expect(req2.request.body).toEqual({});
    expect(req2.request.withCredentials).toBe(true);
    req2.flush(MOCK_LOGOUT_RESPONSE);
    expect(resultDefault).toEqual(MOCK_LOGOUT_RESPONSE);
  });

  it('4. GET /api/auth/me', () => {
    let result: unknown;
    client.authMe().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/auth/me');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_AUTH_ME_RESPONSE);
    expect(result).toEqual(MOCK_AUTH_ME_RESPONSE);
  });

  it('5. PATCH /api/me/preferences', () => {
    let result: unknown;
    client.patchMyPreferences(MOCK_USER_PREFERENCES_PATCH_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/me/preferences');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_USER_PREFERENCES_PATCH_REQUEST);
    req.flush(MOCK_USER_PREFERENCES_RESPONSE);
    expect(result).toEqual(MOCK_USER_PREFERENCES_RESPONSE);
  });

  it('6. GET /api/health', () => {
    let result: unknown;
    client.getHealth().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/health');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_HEALTH_RESPONSE);
    expect(result).toEqual(MOCK_HEALTH_RESPONSE);
  });

  it('7. GET /api/config/public', () => {
    let result: unknown;
    client.getPublicConfig().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/config/public');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_PUBLIC_CONFIG_RESPONSE);
    expect(result).toEqual(MOCK_PUBLIC_CONFIG_RESPONSE);
  });

  it('8. GET /api/map/search with query params q, lang, and limit', () => {
    let result: unknown;
    client
      .searchMapLocations({ q: 'חיפה', lang: 'he', limit: 8 })
      .subscribe((res) => (result = res));

    const req = httpTesting.expectOne((r) => r.url === '/api/map/search');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.params.get('q')).toBe('חיפה');
    expect(req.request.params.get('lang')).toBe('he');
    expect(req.request.params.get('limit')).toBe('8');
    req.flush(MOCK_LOCATION_SEARCH_RESPONSE_HE);
    expect(result).toEqual(MOCK_LOCATION_SEARCH_RESPONSE_HE);
  });

  it('9. GET /api/me/entitlement', () => {
    let result: unknown;
    client.getMyEntitlement().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/me/entitlement');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_ENTITLEMENT_ALLOWED_RESPONSE);
    expect(result).toEqual(MOCK_ENTITLEMENT_ALLOWED_RESPONSE);
  });

  it('10. POST /api/routes', () => {
    let result: unknown;
    client.createRoute(MOCK_ROUTE_CREATE_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/routes');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_ROUTE_CREATE_REQUEST);
    req.flush(MOCK_ROUTE_CREATE_RESPONSE);
    expect(result).toEqual(MOCK_ROUTE_CREATE_RESPONSE);
  });

  it('11. POST /api/uploads/validate sends FormData with binary file', () => {
    let result: unknown;
    const gpxBlob = new Blob(['<gpx version="1.1"></gpx>'], { type: 'application/gpx+xml' });
    client.validateUpload(gpxBlob, 'carmel.gpx').subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/uploads/validate');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toBeInstanceOf(FormData);
    const uploaded = (req.request.body as FormData).get('file');
    expect(uploaded).toBeTruthy();
    req.flush(MOCK_UPLOAD_VALIDATE_RESPONSE);
    expect(result).toEqual(MOCK_UPLOAD_VALIDATE_RESPONSE);
  });

  it('12. POST /api/uploads/{id}/confirm sends Idempotency-Key header', () => {
    let result: unknown;
    const uploadId = MOCK_UPLOAD_CONFIRM_RESPONSE.upload_id;
    client
      .confirmUpload(uploadId, MOCK_UPLOAD_CONFIRM_REQUEST, 'idem-key-123')
      .subscribe((res) => (result = res));

    const req = httpTesting.expectOne(`/api/uploads/${uploadId}/confirm`);
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.headers.get('Idempotency-Key')).toBe('idem-key-123');
    expect(req.request.body).toEqual(MOCK_UPLOAD_CONFIRM_REQUEST);
    req.flush(MOCK_UPLOAD_CONFIRM_RESPONSE, { status: 202, statusText: 'Accepted' });
    expect(result).toEqual(MOCK_UPLOAD_CONFIRM_RESPONSE);
  });

  it('13. DELETE /api/uploads/{id}', () => {
    let result: unknown;
    const uploadId = MOCK_UPLOAD_DELETE_RESPONSE.upload_id;
    client.deleteUploadDraft(uploadId).subscribe((res) => (result = res));

    const req = httpTesting.expectOne(`/api/uploads/${uploadId}`);
    expect(req.request.method).toBe('DELETE');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_UPLOAD_DELETE_RESPONSE);
    expect(result).toEqual(MOCK_UPLOAD_DELETE_RESPONSE);
  });

  it('14. GET /api/me/uploads', () => {
    let result: unknown;
    client.getMyUploads().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/me/uploads');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_MY_UPLOADS_RESPONSE);
    expect(result).toEqual(MOCK_MY_UPLOADS_RESPONSE);
  });

  it('15. GET /api/admin/config', () => {
    let result: unknown;
    client.getAdminConfig().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/admin/config');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_ADMIN_CONFIG_RESPONSE);
    expect(result).toEqual(MOCK_ADMIN_CONFIG_RESPONSE);
  });

  it('16. PUT /api/admin/config', () => {
    let result: unknown;
    client.updateAdminConfig(MOCK_ADMIN_CONFIG_UPDATE_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/admin/config');
    expect(req.request.method).toBe('PUT');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_ADMIN_CONFIG_UPDATE_REQUEST);
    req.flush(MOCK_ADMIN_CONFIG_RESPONSE);
    expect(result).toEqual(MOCK_ADMIN_CONFIG_RESPONSE);
  });

  it('17. GET /api/admin/users', () => {
    let result: unknown;
    client.getAdminUsers().subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/admin/users');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_ADMIN_USERS_RESPONSE);
    expect(result).toEqual(MOCK_ADMIN_USERS_RESPONSE);
  });

  it('18. GET /api/admin/network/edges supports body and observe: response with If-None-Match', () => {
    let bodyResult: unknown;
    client.getAdminNetworkEdges().subscribe((res) => (bodyResult = res));

    const req1 = httpTesting.expectOne('/api/admin/network/edges');
    expect(req1.request.method).toBe('GET');
    expect(req1.request.withCredentials).toBe(true);
    expect(req1.request.headers.has('If-None-Match')).toBe(false);
    req1.flush(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE);
    expect(bodyResult).toEqual(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE);

    let fullResponse: HttpResponse<unknown> | undefined;
    client
      .getAdminNetworkEdges({ ifNoneMatch: '"edges-rev-13"', observe: 'response' })
      .subscribe((res) => (fullResponse = res));

    const req2 = httpTesting.expectOne('/api/admin/network/edges');
    expect(req2.request.headers.get('If-None-Match')).toBe('"edges-rev-13"');
    expect(req2.request.withCredentials).toBe(true);
    req2.flush(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE, {
      headers: new HttpHeaders({ ETag: '"edges-rev-13"' }),
    });
    expect(fullResponse?.headers.get('ETag')).toBe('"edges-rev-13"');
    expect(fullResponse?.body).toEqual(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE);
  });

  it('19. GET /api/admin/network/nodes supports body and observe: response with If-None-Match', () => {
    let bodyResult: unknown;
    client.getAdminNetworkNodes().subscribe((res) => (bodyResult = res));

    const req1 = httpTesting.expectOne('/api/admin/network/nodes');
    expect(req1.request.method).toBe('GET');
    expect(req1.request.withCredentials).toBe(true);
    req1.flush(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE);
    expect(bodyResult).toEqual(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE);

    let fullResponse: HttpResponse<unknown> | undefined;
    client
      .getAdminNetworkNodes({ ifNoneMatch: '"nodes-rev-13"', observe: 'response' })
      .subscribe((res) => (fullResponse = res));

    const req2 = httpTesting.expectOne('/api/admin/network/nodes');
    expect(req2.request.headers.get('If-None-Match')).toBe('"nodes-rev-13"');
    expect(req2.request.withCredentials).toBe(true);
    req2.flush(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE, {
      headers: new HttpHeaders({ ETag: '"nodes-rev-13"' }),
    });
    expect(fullResponse?.headers.get('ETag')).toBe('"nodes-rev-13"');
    expect(fullResponse?.body).toEqual(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE);
  });

  it('20. GET /api/network/edges sends bbox and zoom query params', () => {
    let result: unknown;
    client
      .getNetworkEdges({ bbox: '34.95,32.65,35.10,32.80', zoom: 13 })
      .subscribe((res) => (result = res));

    const req = httpTesting.expectOne((r) => r.url === '/api/network/edges');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.params.get('bbox')).toBe('34.95,32.65,35.10,32.80');
    expect(req.request.params.get('zoom')).toBe('13');
    req.flush(MOCK_NETWORK_EDGES_VIEWPORT_RESPONSE);
    expect(result).toEqual(MOCK_NETWORK_EDGES_VIEWPORT_RESPONSE);
  });

  it('21. GET /api/network/edges/{id}/rating', () => {
    let result: unknown;
    client.getEdgeRating(42).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/network/edges/42/rating');
    expect(req.request.method).toBe('GET');
    expect(req.request.withCredentials).toBe(true);
    req.flush(MOCK_EDGE_RATING_RESPONSE);
    expect(result).toEqual(MOCK_EDGE_RATING_RESPONSE);
  });

  it('22. PUT /api/network/edges/{id}/rating', () => {
    let result: unknown;
    client.putEdgeRating(42, MOCK_EDGE_RATING_PUT_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/network/edges/42/rating');
    expect(req.request.method).toBe('PUT');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_EDGE_RATING_PUT_REQUEST);
    req.flush(MOCK_EDGE_RATING_RESPONSE);
    expect(result).toEqual(MOCK_EDGE_RATING_RESPONSE);
  });

  it('23. POST /api/admin/imports', () => {
    let result: unknown;
    client.startAdminBatchImport(MOCK_BATCH_IMPORT_START_REQUEST).subscribe((res) => (result = res));

    const req = httpTesting.expectOne('/api/admin/imports');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual(MOCK_BATCH_IMPORT_START_REQUEST);
    req.flush(MOCK_BATCH_IMPORT_STATUS_RESPONSE, { status: 202, statusText: 'Accepted' });
    expect(result).toEqual(MOCK_BATCH_IMPORT_STATUS_RESPONSE);
  });

  it('QA edge cases: omits undefined/null search params, supports optional filename, conditional body ETag, and URL-encodes path IDs', () => {
    client.searchMapLocations({ q: 'Carmel', lang: null }).subscribe();
    const searchReq = httpTesting.expectOne((r) => r.url === '/api/map/search');
    expect(searchReq.request.params.get('q')).toBe('Carmel');
    expect(searchReq.request.params.has('lang')).toBe(false);
    expect(searchReq.request.params.has('limit')).toBe(false);
    searchReq.flush(MOCK_LOCATION_SEARCH_RESPONSE_HE);

    const blob = new Blob(['<gpx></gpx>'], { type: 'application/gpx+xml' });
    client.validateUpload(blob).subscribe();
    const uploadReq = httpTesting.expectOne('/api/uploads/validate');
    expect(uploadReq.request.body).toBeInstanceOf(FormData);
    expect((uploadReq.request.body as FormData).get('file')).toBeTruthy();
    uploadReq.flush(MOCK_UPLOAD_VALIDATE_RESPONSE);

    client.getAdminNetworkEdges({ ifNoneMatch: '"rev-9"', observe: 'body' }).subscribe();
    const etagBodyReq = httpTesting.expectOne('/api/admin/network/edges');
    expect(etagBodyReq.request.headers.get('If-None-Match')).toBe('"rev-9"');
    expect(etagBodyReq.request.withCredentials).toBe(true);
    etagBodyReq.flush(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE);

    client.getAdminNetworkNodes({ observe: 'response' }).subscribe();
    const respNoEtagReq = httpTesting.expectOne('/api/admin/network/nodes');
    expect(respNoEtagReq.request.headers.has('If-None-Match')).toBe(false);
    respNoEtagReq.flush(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE);

    client.deleteUploadDraft('id/with space').subscribe();
    const encodedReq = httpTesting.expectOne('/api/uploads/id%2Fwith%20space');
    expect(encodedReq.request.method).toBe('DELETE');
    encodedReq.flush(MOCK_UPLOAD_DELETE_RESPONSE);
  });
});
