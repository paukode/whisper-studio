/**
 * All localStorage key strings used by the app.
 * Centralised here to avoid magic strings scattered across modules.
 */

export const STORAGE_KEYS = {
  /** Selected theme key */
  THEME: 'whisper_theme',
  /** Sidebar collapsed boolean */
  SIDEBAR_COLLAPSED: 'whisper_sidebar_collapsed',
  /** Workspace connect dialog size + position (resizable popup) */
  WS_DIALOG_GEOMETRY: 'whisper_ws_dialog_geometry',
} as const;

export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS];
