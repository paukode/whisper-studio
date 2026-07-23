import { create } from 'zustand';

/**
 * Per-session selection of which indexed workspaces the chat should search
 * (point D). The selected paths are sent in the chat body as
 * `selected_search_indexes`; when non-empty the backend grounds the answer in
 * them first (point I). Default is "all indexed" — the composer initializes a
 * session's entry to every indexed path the first time the list loads.
 *
 * Kept out of the core session record (lower risk) but persisted to
 * localStorage so a session's choice survives a reload.
 */
const KEY = 'whisper-index-search-selection';

function load(): Record<string, string[]> {
  try {
    return JSON.parse(localStorage.getItem(KEY) || '{}');
  } catch {
    return {};
  }
}

function persist(v: Record<string, string[]>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(v));
  } catch {
    /* storage full / unavailable — selection just won't survive reload */
  }
}

interface IndexSearchState {
  selectionBySession: Record<string, string[]>;
  setSelection: (sessionId: string, paths: string[]) => void;
  clearSession: (sessionId: string) => void;
}

export const useIndexSearchStore = create<IndexSearchState>()((set, get) => ({
  selectionBySession: load(),
  setSelection: (sessionId, paths) => {
    const next = { ...get().selectionBySession, [sessionId]: paths };
    persist(next);
    set({ selectionBySession: next });
  },
  clearSession: (sessionId) => {
    if (!(sessionId in get().selectionBySession)) return;
    const next = { ...get().selectionBySession };
    delete next[sessionId];
    persist(next);
    set({ selectionBySession: next });
  },
}));
