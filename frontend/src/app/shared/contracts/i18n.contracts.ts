import type {
  ContributionLevelKey,
  DifficultyLevel,
  ErrorCode,
  PreferredLanguage,
  RejectionCode,
} from '../../api-client/models';

export type SupportedLanguage = PreferredLanguage;

export const DEFAULT_LANGUAGE: SupportedLanguage = 'he';

export type TextDirection = 'rtl' | 'ltr';

export const LANGUAGE_DIRECTION_MAP: Readonly<Record<SupportedLanguage, TextDirection>> = {
  he: 'rtl',
  en: 'ltr',
};

export const I18N_SCOPES = ['shared', 'auth', 'map', 'upload', 'admin'] as const;

export type I18nScope = (typeof I18N_SCOPES)[number];

export const LANGUAGE_STORAGE_KEY = 'ontrack.preferred_language';
export const LOGOUT_PENDING_STORAGE_KEY = 'ontrack.logout_pending';
export const AUTH_BROADCAST_CHANNEL = 'ontrack.auth';

const VALID_ERROR_CODE_PATTERN = /^[A-Z0-9_]+$/;

export function errorCodeToI18nKey(code: ErrorCode | string): string {
  const normalized = code.trim();
  if (!normalized || !VALID_ERROR_CODE_PATTERN.test(normalized)) {
    return 'shared.errors.INTERNAL_ERROR';
  }
  return `shared.errors.${normalized}`;
}

export function rejectionCodeToI18nKey(code: RejectionCode): string {
  return `upload.rejections.${code}`;
}

export function contributionLevelToI18nKey(badgeKey: ContributionLevelKey): string {
  return `shared.contributionLevels.${badgeKey}`;
}

export function difficultyLevelToI18nKey(level: DifficultyLevel): string {
  return `shared.difficulty.level${level}`;
}
