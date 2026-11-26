import type {
  ContributionLevelInfo,
  CoordinateSchema,
  DifficultyLevel,
  EdgeRatingResponse,
  LocationSearchResultSchema,
} from '../../api-client/models';
import type { SupportedLanguage } from './i18n.contracts';

export const DIFFICULTY_COLORS: Readonly<Record<DifficultyLevel, string>> = {
  1: '#2e7d32',
  2: '#ef6c00',
  3: '#c62828',
};

export function difficultyColor(level: DifficultyLevel): string {
  return DIFFICULTY_COLORS[level];
}

export interface ExternalConnectorStyle {
  readonly color: string;
  readonly weight: number;
  readonly opacity: number;
  readonly dashArray: string;
  readonly passabilityVerified: false;
}

export const EXTERNAL_CONNECTOR_STYLE: ExternalConnectorStyle = {
  color: '#607d8b',
  weight: 4,
  opacity: 0.85,
  dashArray: '8 6',
  passabilityVerified: false,
};

/**
 * Map a continuous effective difficulty average in [1.0, 3.0] to its discrete
 * display level (SPEC AC-34 & edge_difficulty SQL view):
 * - avg < 1.5 -> 1
 * - avg < 2.5 -> 2
 * - avg >= 2.5 -> 3
 */
export function difficultyToDisplayLevel(avg: number): DifficultyLevel {
  if (!Number.isFinite(avg) || avg < 1 || avg > 3) {
    throw new RangeError(`Expected finite difficulty average in [1, 3], got ${String(avg)}.`);
  }
  if (avg < 1.5) {
    return 1;
  }
  if (avg < 2.5) {
    return 2;
  }
  return 3;
}

export const ISRAEL_DEFAULT_CENTER: Readonly<CoordinateSchema> = {
  lat: 32.6,
  lng: 35.0,
};

export const MIN_MAP_ZOOM = 7;
export const MAX_MAP_ZOOM = 16;
export const DEFAULT_MAP_ZOOM = 13;

export interface MapZoomBounds {
  readonly min: number;
  readonly max: number;
  readonly defaultZoom: number;
}

export const MAP_ZOOM_BOUNDS: MapZoomBounds = {
  min: MIN_MAP_ZOOM,
  max: MAX_MAP_ZOOM,
  defaultZoom: DEFAULT_MAP_ZOOM,
};

export type MapInteractionMode = 'plan' | 'rate';

export interface LogoutPendingRecord {
  readonly sessionId: string | null;
  readonly failedAt: string;
  readonly attempts: number;
}

export type AuthBroadcastEvent =
  | { readonly type: 'LOGIN'; readonly sessionId: string }
  | { readonly type: 'LOGOUT'; readonly sessionId: string | null }
  | { readonly type: 'LANGUAGE_CHANGED'; readonly language: SupportedLanguage };

export type ComingSoonFeatureKey =
  | 'save_route'
  | 'my_routes'
  | 'add_waypoint'
  | 'ai_route_planner';

export interface DifficultyLegendProps {
  readonly showExternalConnectorNote: boolean;
  readonly compact?: boolean;
}

export interface ContributionBadgeProps {
  readonly contributionLevel: ContributionLevelInfo;
  readonly showProgress?: boolean;
}

export interface ComingSoonControlProps {
  readonly featureKey: ComingSoonFeatureKey;
  readonly tooltipKey?: string;
}

export interface HealthBannerState {
  readonly healthy: boolean;
  readonly lastCheckedAt: string | null;
  readonly degradedSince: string | null;
}

export interface LoadingOverlayState {
  readonly active: boolean;
  readonly messageKey?: string;
}

export interface MapSearchSelectionEvent {
  readonly result: LocationSearchResultSchema;
  readonly action: 'focus' | 'set_start' | 'set_finish';
}

export interface EdgeRatingDialogData {
  readonly edgeId: number;
  readonly avgDifficulty: number;
  readonly displayLevel: DifficultyLevel;
  readonly reportCount: number;
  readonly userRating: DifficultyLevel | null;
}

export interface EdgeRatingSubmittedEvent {
  readonly edgeId: number;
  readonly difficulty: DifficultyLevel;
  readonly response: EdgeRatingResponse;
}
