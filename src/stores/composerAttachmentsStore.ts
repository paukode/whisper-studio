import { create } from 'zustand';
import type { ComposerAttachment } from '@/hooks/useComposerAttachments';

/**
 * Per-session composer attachment chips, keyed by session id.
 *
 * The composer (ChatInput) is intentionally never remounted on a session
 * switch (see the rationale comment in AppShell), so unkeyed component-local
 * state carries over: a file attached in one session would show up, still
 * mid-upload, in another session's composer. Keying the chips by session id
 * here isolates them, the same pattern indexSearchStore uses for its
 * per-session selection.
 *
 * In-memory only (not persisted). Chips reference attachment records the
 * backend just created for this session; surviving a reload is neither
 * expected nor safe, so unlike indexSearchStore there is no localStorage.
 */
type Updater =
  | ComposerAttachment[]
  | ((prev: ComposerAttachment[]) => ComposerAttachment[]);

interface ComposerAttachmentsState {
  bySession: Record<string, ComposerAttachment[]>;
  /** Apply a value or functional updater to one session's chips. Mirrors a
   *  React setState so the hook can expose an identical setAttachments. */
  update: (sessionId: string, updater: Updater) => void;
  /** Drop a session's chips (called from session delete cleanup). */
  clearSession: (sessionId: string) => void;
}

/** Stable empty array so the selector returns a referentially stable value
 *  for sessions with no chips, avoiding a re-render loop. */
export const EMPTY_ATTACHMENTS: ComposerAttachment[] = [];

export const useComposerAttachmentsStore = create<ComposerAttachmentsState>()((set, get) => ({
  bySession: {},
  update: (sessionId, updater) => {
    const prev = get().bySession[sessionId] ?? EMPTY_ATTACHMENTS;
    const next = typeof updater === 'function' ? updater(prev) : updater;
    if (next === prev) return;
    set({ bySession: { ...get().bySession, [sessionId]: next } });
  },
  clearSession: (sessionId) => {
    if (!(sessionId in get().bySession)) return;
    const next = { ...get().bySession };
    delete next[sessionId];
    set({ bySession: next });
  },
}));
