// AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.
// Regenerate via: cd backend && .venv/bin/python -m app.contracts.codegen

import { HttpClient, HttpHeaders, HttpParams, HttpResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import type { Observable } from 'rxjs';
import type {
  AdminConfigResponse,
  AdminConfigUpdateRequest,
  AdminNetworkEdgesGeoJsonResponse,
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
} from './models';

export interface ConditionalGetBodyOptions {
  ifNoneMatch?: string;
  observe?: 'body';
}

export interface ConditionalGetResponseOptions {
  ifNoneMatch?: string;
  observe: 'response';
}

export interface SearchMapLocationsParams {
  q: string;
  lang?: 'he' | 'en' | null;
  limit?: number;
}

export interface GetNetworkEdgesParams {
  bbox: string;
  zoom: number;
}

@Injectable({ providedIn: 'root' })
export class OnTrackApiClient {
  private readonly http = inject(HttpClient);

  getAdminConfig(): Observable<AdminConfigResponse> {
    return this.http.get<AdminConfigResponse>('/api/admin/config', { withCredentials: true });
  }

  updateAdminConfig(body: AdminConfigUpdateRequest): Observable<AdminConfigResponse> {
    return this.http.put<AdminConfigResponse>('/api/admin/config', body, { withCredentials: true });
  }

  startAdminBatchImport(body: BatchImportStartRequest): Observable<BatchImportStatusResponse> {
    return this.http.post<BatchImportStatusResponse>('/api/admin/imports', body, { withCredentials: true });
  }

  getAdminNetworkEdges(
    options?: ConditionalGetBodyOptions,
  ): Observable<AdminNetworkEdgesGeoJsonResponse>;
  getAdminNetworkEdges(
    options: ConditionalGetResponseOptions,
  ): Observable<HttpResponse<AdminNetworkEdgesGeoJsonResponse>>;
  getAdminNetworkEdges(
    options?: ConditionalGetBodyOptions | ConditionalGetResponseOptions,
  ): Observable<AdminNetworkEdgesGeoJsonResponse | HttpResponse<AdminNetworkEdgesGeoJsonResponse>> {
    let headers = new HttpHeaders();
    if (options?.ifNoneMatch !== undefined) {
      headers = headers.set('If-None-Match', options.ifNoneMatch);
    }
    if (options?.observe === 'response') {
      return this.http.get<AdminNetworkEdgesGeoJsonResponse>('/api/admin/network/edges', {
        headers,
        observe: 'response',
        withCredentials: true,
      });
    }
    return this.http.get<AdminNetworkEdgesGeoJsonResponse>('/api/admin/network/edges', {
      headers,
      withCredentials: true,
    });
  }

  getAdminNetworkNodes(
    options?: ConditionalGetBodyOptions,
  ): Observable<AdminNetworkNodesGeoJsonResponse>;
  getAdminNetworkNodes(
    options: ConditionalGetResponseOptions,
  ): Observable<HttpResponse<AdminNetworkNodesGeoJsonResponse>>;
  getAdminNetworkNodes(
    options?: ConditionalGetBodyOptions | ConditionalGetResponseOptions,
  ): Observable<AdminNetworkNodesGeoJsonResponse | HttpResponse<AdminNetworkNodesGeoJsonResponse>> {
    let headers = new HttpHeaders();
    if (options?.ifNoneMatch !== undefined) {
      headers = headers.set('If-None-Match', options.ifNoneMatch);
    }
    if (options?.observe === 'response') {
      return this.http.get<AdminNetworkNodesGeoJsonResponse>('/api/admin/network/nodes', {
        headers,
        observe: 'response',
        withCredentials: true,
      });
    }
    return this.http.get<AdminNetworkNodesGeoJsonResponse>('/api/admin/network/nodes', {
      headers,
      withCredentials: true,
    });
  }

  getAdminUsers(): Observable<AdminUsersResponse> {
    return this.http.get<AdminUsersResponse>('/api/admin/users', { withCredentials: true });
  }

  authLogin(body: LoginRequest): Observable<LoginResponse> {
    return this.http.post<LoginResponse>('/api/auth/login', body, { withCredentials: true });
  }

  authLogout(body?: LogoutRequest | null): Observable<LogoutResponse> {
    return this.http.post<LogoutResponse>('/api/auth/logout', body ?? {}, { withCredentials: true });
  }

  authMe(): Observable<AuthMeResponse> {
    return this.http.get<AuthMeResponse>('/api/auth/me', { withCredentials: true });
  }

  authRegister(body: RegisterRequest): Observable<RegisterResponse> {
    return this.http.post<RegisterResponse>('/api/auth/register', body, { withCredentials: true });
  }

  getPublicConfig(): Observable<PublicConfigResponse> {
    return this.http.get<PublicConfigResponse>('/api/config/public', { withCredentials: true });
  }

  getHealth(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>('/api/health', { withCredentials: true });
  }

  searchMapLocations(params: SearchMapLocationsParams): Observable<LocationSearchResponse> {
    let httpParams = new HttpParams();
    httpParams = httpParams.set('q', String(params.q));
    if (params.lang !== undefined && params.lang !== null) {
      httpParams = httpParams.set('lang', String(params.lang));
    }
    if (params.limit !== undefined && params.limit !== null) {
      httpParams = httpParams.set('limit', String(params.limit));
    }
    return this.http.get<LocationSearchResponse>('/api/map/search', { params: httpParams, withCredentials: true });
  }

  getMyEntitlement(): Observable<EntitlementResponse> {
    return this.http.get<EntitlementResponse>('/api/me/entitlement', { withCredentials: true });
  }

  patchMyPreferences(body: UserPreferencesPatchRequest): Observable<UserPreferencesResponse> {
    return this.http.patch<UserPreferencesResponse>('/api/me/preferences', body, { withCredentials: true });
  }

  getMyUploads(): Observable<MyUploadsResponse> {
    return this.http.get<MyUploadsResponse>('/api/me/uploads', { withCredentials: true });
  }

  getNetworkEdges(params: GetNetworkEdgesParams): Observable<NetworkEdgesViewportResponse> {
    let httpParams = new HttpParams();
    httpParams = httpParams.set('bbox', String(params.bbox));
    httpParams = httpParams.set('zoom', String(params.zoom));
    return this.http.get<NetworkEdgesViewportResponse>('/api/network/edges', { params: httpParams, withCredentials: true });
  }

  getEdgeRating(id: number): Observable<EdgeRatingResponse> {
    return this.http.get<EdgeRatingResponse>(`/api/network/edges/${encodeURIComponent(String(id))}/rating`, { withCredentials: true });
  }

  putEdgeRating(id: number, body: EdgeRatingPutRequest): Observable<EdgeRatingResponse> {
    return this.http.put<EdgeRatingResponse>(`/api/network/edges/${encodeURIComponent(String(id))}/rating`, body, { withCredentials: true });
  }

  createRoute(body: RouteCreateRequest): Observable<RouteCreateResponse> {
    return this.http.post<RouteCreateResponse>('/api/routes', body, { withCredentials: true });
  }

  validateUpload(file: Blob | File, filename?: string): Observable<UploadValidateResponse> {
    const formData = new FormData();
    if (filename !== undefined) {
      formData.append('file', file, filename);
    } else {
      formData.append('file', file);
    }
    return this.http.post<UploadValidateResponse>('/api/uploads/validate', formData, { withCredentials: true });
  }

  deleteUploadDraft(id: string): Observable<UploadDeleteResponse> {
    return this.http.delete<UploadDeleteResponse>(`/api/uploads/${encodeURIComponent(String(id))}`, { withCredentials: true });
  }

  confirmUpload(id: string, body: UploadConfirmRequest, idempotencyKey: string): Observable<UploadConfirmResponse> {
    let headers = new HttpHeaders();
    headers = headers.set('Idempotency-Key', idempotencyKey);
    return this.http.post<UploadConfirmResponse>(`/api/uploads/${encodeURIComponent(String(id))}/confirm`, body, { headers, withCredentials: true });
  }
}
