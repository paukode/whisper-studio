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
  /** Git diff viewer size + position (resizable popup) */
  GIT_DIFF_GEOMETRY: 'whisper_git_diff_geometry',
  /** Git diff viewer layout: 'split' or 'unified' */
  GIT_DIFF_LAYOUT: 'whisper_git_diff_layout',
} as const;
