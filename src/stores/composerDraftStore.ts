import { create } from 'zustand';

/**
 * Per-session composer drafts, keyed by session id.
 *
 * The composer (ChatInput) is never remounted on a session switch (see the
 * rationale comment in AppShell), so text held in component state carried
 * over: words typed in one session showed in every other session's composer.
 * Keying the draft by session id isolates it, the same pattern
 * composerAttachmentsStore uses for the chips. The welcome screen, before a
 * session exists, uses DRAFT_SESSION.
 *
 * Unlike the chips, a draft is plain text the user typed, so it is kept in
 * localStorage as well: it survives a reload and comes back when the user
 * returns to the session.
 */
const STORAGE_KEY = 'whisper_composer_drafts';

type Updater = string | ((prev: string) => string);

function load(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        (e): e is [string, string] => typeof e[1] === 'string' && e[1] !== '',
      ),
    );
  } catch {
    return {};
  }
}

function persist(drafts: Record<string, string>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(drafts));
  } catch {
    // Storage blocked or full: the draft still lives for this page load.
  }
}

interface ComposerDraftState {
  bySession: Record<string, string>;
  /** Apply a value or functional updater to one session's draft. Mirrors a
   *  React setState so the hook can expose an identical setText. */
  update: (sessionId: string, updater: Updater) => void;
  /** Drop a session's draft (called from session delete cleanup). */
  clearSession: (sessionId: string) => void;
  /** Move the draft held under `from` to `to`, unless `to` has one already. */
  adopt: (from: string, to: string) => void;
}

export const useComposerDraftStore = create<ComposerDraftState>()((set, get) => ({
  bySession: load(),
  update: (sessionId, updater) => {
    const prev = get().bySession[sessionId] ?? '';
    const next = typeof updater === 'function' ? updater(prev) : updater;
    if (next === prev) return;
    const bySession = { ...get().bySession };
    if (next) bySession[sessionId] = next;
    else delete bySession[sessionId];
    set({ bySession });
    persist(bySession);
  },
  adopt: (from, to) => {
    const { bySession } = get();
    const draft = bySession[from];
    if (!draft || bySession[to]) return;
    const next = { ...bySession, [to]: draft };
    delete next[from];
    set({ bySession: next });
    persist(next);
  },
  clearSession: (sessionId) => {
    if (!(sessionId in get().bySession)) return;
    const bySession = { ...get().bySession };
    delete bySession[sessionId];
    set({ bySession });
    persist(bySession);
  },
}));
