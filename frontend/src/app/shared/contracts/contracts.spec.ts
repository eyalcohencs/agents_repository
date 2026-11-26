import { describe, expect, it } from 'vitest';
import {
  AUTH_BROADCAST_CHANNEL,
  DEFAULT_LANGUAGE,
  DEFAULT_MAP_ZOOM,
  DIFFICULTY_COLORS,
  EXTERNAL_CONNECTOR_STYLE,
  I18N_SCOPES,
  ISRAEL_DEFAULT_CENTER,
  LANGUAGE_DIRECTION_MAP,
  LANGUAGE_STORAGE_KEY,
  LOGOUT_PENDING_STORAGE_KEY,
  MAP_ZOOM_BOUNDS,
  MAX_MAP_ZOOM,
  MIN_MAP_ZOOM,
  MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE,
  MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE,
  MOCK_ADMIN_USERS_RESPONSE,
  MOCK_LOCATION_SEARCH_RESPONSE_EN,
  MOCK_LOCATION_SEARCH_RESPONSE_HE,
  MOCK_MY_UPLOADS_RESPONSE,
  MOCK_ROUTE_CREATE_RESPONSE,
  contributionLevelToI18nKey,
  difficultyColor,
  difficultyLevelToI18nKey,
  difficultyToDisplayLevel,
  errorCodeToI18nKey,
  rejectionCodeToI18nKey,
} from './index';
import type {
  AuthBroadcastEvent,
  ComingSoonFeatureKey,
  LogoutPendingRecord,
  MapInteractionMode,
} from './index';

describe('shared/contracts/i18n.contracts', () => {
  it('defaults to Hebrew RTL and maps languages to directions', () => {
    expect(DEFAULT_LANGUAGE).toBe('he');
    expect(LANGUAGE_DIRECTION_MAP.he).toBe('rtl');
    expect(LANGUAGE_DIRECTION_MAP.en).toBe('ltr');
    expect(I18N_SCOPES).toEqual(['shared', 'auth', 'map', 'upload', 'admin']);
    expect(LANGUAGE_STORAGE_KEY).toBe('ontrack.preferred_language');
    expect(LOGOUT_PENDING_STORAGE_KEY).toBe('ontrack.logout_pending');
    expect(AUTH_BROADCAST_CHANNEL).toBe('ontrack.auth');
  });

  it('maps error, rejection, contribution-level, and difficulty codes to stable i18n keys', () => {
    expect(errorCodeToI18nKey('ROUTE_NOT_ALLOWED')).toBe('shared.errors.ROUTE_NOT_ALLOWED');
    expect(errorCodeToI18nKey('INVALID_CREDENTIALS')).toBe('shared.errors.INVALID_CREDENTIALS');
    expect(errorCodeToI18nKey('')).toBe('shared.errors.INTERNAL_ERROR');
    expect(errorCodeToI18nKey('bad code!')).toBe('shared.errors.INTERNAL_ERROR');

    expect(rejectionCodeToI18nKey('DEGENERATE_GEOMETRY')).toBe(
      'upload.rejections.DEGENERATE_GEOMETRY',
    );
    expect(rejectionCodeToI18nKey('OUTSIDE_SERVICE_AREA')).toBe(
      'upload.rejections.OUTSIDE_SERVICE_AREA',
    );

    expect(contributionLevelToI18nKey('explorer')).toBe('shared.contributionLevels.explorer');
    expect(contributionLevelToI18nKey('trail_master')).toBe(
      'shared.contributionLevels.trail_master',
    );

    expect(difficultyLevelToI18nKey(1)).toBe('shared.difficulty.level1');
    expect(difficultyLevelToI18nKey(2)).toBe('shared.difficulty.level2');
    expect(difficultyLevelToI18nKey(3)).toBe('shared.difficulty.level3');
  });
});

describe('shared/contracts/ui.contracts', () => {
  it('exposes exact FR-023 difficulty colors and neutral external connector style', () => {
    expect(DIFFICULTY_COLORS).toEqual({
      1: '#2e7d32',
      2: '#ef6c00',
      3: '#c62828',
    });
    expect(difficultyColor(1)).toBe('#2e7d32');
    expect(difficultyColor(2)).toBe('#ef6c00');
    expect(difficultyColor(3)).toBe('#c62828');

    expect(EXTERNAL_CONNECTOR_STYLE.passabilityVerified).toBe(false);
    expect(EXTERNAL_CONNECTOR_STYLE.dashArray.length).toBeGreaterThan(0);
  });

  it('rounds continuous difficulty averages matching SPEC AC-34 boundaries', () => {
    expect(difficultyToDisplayLevel(1.0)).toBe(1);
    expect(difficultyToDisplayLevel(1.333)).toBe(1);
    expect(difficultyToDisplayLevel(1.4999)).toBe(1);
    expect(difficultyToDisplayLevel(1.5)).toBe(2);
    expect(difficultyToDisplayLevel(2.0)).toBe(2);
    expect(difficultyToDisplayLevel(2.4999)).toBe(2);
    expect(difficultyToDisplayLevel(2.5)).toBe(3);
    expect(difficultyToDisplayLevel(3.0)).toBe(3);
  });

  it('rejects out-of-range and non-finite difficulty averages', () => {
    for (const invalid of [0, 0.99, 3.01, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => difficultyToDisplayLevel(invalid)).toThrow(RangeError);
    }
  });

  it('defines Israel FR-005 map defaults, zoom bounds, and interaction/session types', () => {
    expect(ISRAEL_DEFAULT_CENTER).toEqual({ lat: 32.6, lng: 35.0 });
    expect(MIN_MAP_ZOOM).toBe(7);
    expect(MAX_MAP_ZOOM).toBe(16);
    expect(DEFAULT_MAP_ZOOM).toBe(13);
    expect(MAP_ZOOM_BOUNDS).toEqual({ min: 7, max: 16, defaultZoom: 13 });

    const modes: MapInteractionMode[] = ['plan', 'rate'];
    expect(modes).toEqual(['plan', 'rate']);

    const comingSoon: ComingSoonFeatureKey[] = [
      'save_route',
      'my_routes',
      'add_waypoint',
      'ai_route_planner',
    ];
    expect(comingSoon).toHaveLength(4);

    const record: LogoutPendingRecord = {
      sessionId: '11111111-1111-4111-8111-111111111111',
      failedAt: '2026-11-26T10:00:00Z',
      attempts: 1,
    };
    const event: AuthBroadcastEvent = { type: 'LOGOUT', sessionId: record.sessionId };
    expect(event.type).toBe('LOGOUT');
  });
});

describe('shared/contracts/mock-fixtures', () => {
  it('provides bilingual Hebrew/English Israel fixtures matching frozen API models', () => {
    expect(MOCK_LOCATION_SEARCH_RESPONSE_HE.query).toBe('חיפה');
    expect(MOCK_LOCATION_SEARCH_RESPONSE_HE.results[0]?.lat).toBeGreaterThan(29);
    expect(MOCK_LOCATION_SEARCH_RESPONSE_HE.results[0]?.lng).toBeGreaterThan(34);
    expect(MOCK_LOCATION_SEARCH_RESPONSE_EN.results[0]?.display_name).toContain('Israel');

    expect(MOCK_ROUTE_CREATE_RESPONSE.segments.some((s) => s.source === 'network')).toBe(true);
    expect(MOCK_ROUTE_CREATE_RESPONSE.segments.some((s) => s.source === 'external')).toBe(true);
    expect(MOCK_MY_UPLOADS_RESPONSE.uploads[0]?.latest_ingestion_run?.source_type).toBe('upload');
    expect(MOCK_ADMIN_NETWORK_EDGES_GEOJSON_RESPONSE.features[0]?.geometry.type).toBe('LineString');
    expect(MOCK_ADMIN_NETWORK_NODES_GEOJSON_RESPONSE.features[0]?.geometry.type).toBe('Point');
    expect(MOCK_ADMIN_USERS_RESPONSE.users).toHaveLength(2);
  });
});
