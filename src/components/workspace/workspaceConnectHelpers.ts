// Shared types and pure helpers for the workspace connect dialog and its
// browser / recent-item subcomponents. No React, no state.

// On-device model for typed-relation extraction (mirrors server _LOCAL_MODEL_KEY).
export const LOCAL_RELATIONS_MODEL = 'local_gemma';

export interface BrowseEntry {
  name: string;
  mtime: number;
}

export interface BrowseResponse {
  current: string;
  parent: string | null;
  dirs: string[];
  /** Rich shape with mtimes (newer backends); `dirs` kept as fallback. */
  entries?: BrowseEntry[];
  /** Files in the folder (newer backends), capped at the server's FILE_CAP. */
  files?: BrowseEntry[];
  /** Total file count before the cap, so the UI can show "showing N of M". */
  file_total?: number;
}

export type SortMode = 'name-asc' | 'name-desc' | 'mtime-desc' | 'mtime-asc';

export const SORT_LABELS: Record<SortMode, string> = {
  'name-asc': 'Name A→Z',
  'name-desc': 'Name Z→A',
  'mtime-desc': 'Newest first',
  'mtime-asc': 'Oldest first',
};

export function formatMtime(mtime: number): string {
  if (!mtime) return '';
  const d = new Date(mtime * 1000);
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}-${mm}-${d.getFullYear()}`;
}

/** Strip trailing slashes so a typed "/x/y/" matches the realpath-normalized
 *  "/x/y" that connect stores in recents. Browsed-in paths are already
 *  canonical (the server returns realpath), so this only matters for typed or
 *  pasted paths. (Symlinked typed paths can still diverge from the index's
 *  abspath key — a deeper backend inconsistency left out of this change.) */
export function normalizeWsPath(p: string): string {
  const t = p.trim().replace(/\/+$/, '');
  return t || p.trim();
}

/** "2h ago" style relative time for the last-indexed badge. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
