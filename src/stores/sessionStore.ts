import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type { Session } from '@/types/session';
import type { ChatMessage } from '@/types/chat';
import * as sessionsApi from '@/api/sessions';
import { SessionListResponseSchema } from '@/types/schemas';
import {
  dropRuntime,
  getChatStore,
  getRuntime,
  getTranscriptionStore,
  maybeEvictIdle,
} from './sessionRuntimes';
import { useRecordingStore } from './recordingStore';
import { useWorkspaceStore } from './workspaceStore';
import { useUIStore } from './uiStore';
import { useIndexSearchStore } from './indexSearchStore';
import { useCronUnreadStore } from './cronUnreadStore';

/** Deleting a session that owns the live recording must stop the engine
 *  first (websocket, mic worklet, watchdog), or it would keep streaming
 *  audio for a session that no longer exists. Lazy import avoids a
 *  store ↔ service module cycle at init time. */
async function stopRecordingIfOwner(sessionId: string): Promise<void> {
  if (useRecordingStore.getState().recordingSessionId !== sessionId) return;
  const { recordingController } = await import('@/services/recordingController');
  recordingController.stop();
}

/** Summary returned by GET /api/sessions (list) */
interface SessionSummary {
  id: string;
  title: string;
  date: string;
  segmentCount: number;
  chatCount: number;
  workspacePath: string;
  pinned: boolean;
  archived: boolean;
}

export interface SessionState {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  /** Metadata for every session with a live runtime (parallel sessions).
   *  Replaces the old single `currentSession` — per-session saves need
   *  per-session metadata (createdAt, customTitle, …). */
  liveSessions: Record<string, Session>;
  isLoading: boolean;

  /* Actions */
  loadSessions: () => Promise<void>;
  createSession: () => string;
  switchSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  bulkDeleteSessions: (ids: string[]) => Promise<void>;
  setSessionFlags: (id: string, flags: { pinned?: boolean; archived?: boolean }) => void;
  branchSession: (id: string) => Promise<void>;
  updateSessionTitle: (id: string, title: string, custom: boolean) => void;
  /** Persist one session's full state (chat + transcript from its own
   *  runtime stores). Returns the underlying update promise so callers
   *  that need durability (e.g. branchSession) can await the flush;
   *  fire-and-forget callers may ignore it. Resolves to nothing when the
   *  session has no live metadata. */
  saveSession: (id: string) => Promise<{ ok: boolean }> | void;
  debouncedSave: (id: string) => void;
  syncChatHistory: (id: string, messages: ChatMessage[]) => void;
  dropLiveSession: (id: string) => void;
  clearChat: () => void;
}

/** Per-session debounce timers — A's pending save must not be cancelled
 *  by B's keystroke. */
const saveTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

function normalizeSession(session: Session): Session {
  // Backend returns "date" instead of "updatedAt" — normalize.
  const backendDate = 'date' in session ? String((session as Record<string, unknown>).date ?? '') : '';
  return {
    id: session.id,
    title: session.title || 'Untitled',
    customTitle: session.customTitle ?? false,
    generatedTitle: session.generatedTitle ?? false,
    createdAt: session.createdAt || backendDate || '',
    updatedAt: backendDate || session.updatedAt || '',
    segments: session.segments ?? [],
    chatHistory: session.chatHistory ?? [],
    speakerNames: session.speakerNames ?? {},
  };
}

/**
 * Persist `currentSessionId` only. Sessions, live session content, and
 * loading state come from the backend on mount — persisting them in
 * localStorage would race with server-side updates.
 *
 * Why persist anything? When the assistant runs a file-mutating git
 * command (checkout, merge, stash apply, reset --hard) in the workspace
 * Vite is serving, the file watcher sees many .ts/.tsx files change at
 * once, triggers an HMR cascade, and any non-HMR-able module (e.g. a
 * type-only file like src/types/chat.ts) forces a full page reload.
 * Without persistence, the chat session is lost on that reload. With
 * persistence, useSessionPersistence sees the saved id on mount and
 * calls switchSession(id) to repopulate from the backend.
 */
export const useSessionStore = create<SessionState>()(persist((set, get) => ({
  sessions: [],
  currentSessionId: null,
  liveSessions: {},
  isLoading: false,

  loadSessions: async () => {
    set({ isLoading: true });
    try {
      const raw = await sessionsApi.getSessions();
      const parsed = SessionListResponseSchema.safeParse(raw);
      const summaries: SessionSummary[] = parsed.success
        ? parsed.data.map((s) => ({
            id: s.id,
            title: s.title,
            date: s.date,
            segmentCount: s.segmentCount,
            chatCount: s.chatCount,
            workspacePath: s.workspacePath,
            pinned: s.pinned,
            archived: s.archived,
          }))
        : (Array.isArray(raw) ? raw : []).map((s) => ({
            id: s.id,
            title: s.title || 'Untitled',
            date: ('date' in s ? String((s as Record<string, unknown>).date ?? '') : ''),
            segmentCount: 0,
            chatCount: s.chatHistory?.length ?? 0,
            workspacePath: '',
            pinned: false,
            archived: false,
          }));
      // Sort by date descending (most recent first)
      summaries.sort((a, b) => {
        if (!a.date && !b.date) return 0;
        if (!a.date) return 1;
        if (!b.date) return -1;
        return new Date(b.date).getTime() - new Date(a.date).getTime();
      });
      set({ sessions: summaries });
    } catch (err) {
      console.warn('Failed to load sessions:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load sessions',
        duration: 4000,
      });
    } finally {
      set({ isLoading: false });
    }
  },

  createSession: () => {
    const id = crypto.randomUUID();
    const now = new Date().toISOString();
    const newSession: Session = {
      id,
      title: 'New Session',
      customTitle: false,
      generatedTitle: false,
      createdAt: now,
      updatedAt: now,
      segments: [],
      chatHistory: [],
      speakerNames: {},
    };

    // Save to backend
    void sessionsApi.createSession(newSession);

    const summary: SessionSummary = {
      id,
      title: 'New Session',
      date: now,
      segmentCount: 0,
      chatCount: 0,
      workspacePath: '',
      pinned: false,
      archived: false,
    };

    // A brand-new session's runtime starts empty by definition — mark it
    // hydrated so switching to it never fetches.
    const entry = getRuntime(id);
    entry.hydrated = true;

    set((state) => ({
      sessions: [summary, ...state.sessions],
      currentSessionId: id,
      liveSessions: { ...state.liveSessions, [id]: newSession },
    }));

    // Note: a live recording deliberately SURVIVES new-session creation
    // now — it is bound to its owning session's store, so the fresh
    // session can't receive its transcripts. The old stop-recording
    // dispatch existed only to protect the singleton store.

    // New session starts with a clean editor surface (tabs persist across
    // SWITCHES, but a fresh session means fresh context).
    const ws = useWorkspaceStore.getState();
    for (const tab of ws.editorTabs) {
      ws.closeTab(tab.path);
    }

    maybeEvictIdle();
    return id;
  },

  switchSession: async (id: string) => {
    const { currentSessionId } = get();
    if (id === currentSessionId) return;

    // Nothing gets lost: immediate background save of the session we're
    // leaving (its runtime stays alive — streams/recording continue).
    if (currentSessionId) get().saveSession(currentSessionId);

    const entry = getRuntime(id);
    entry.lastUsed = Date.now();

    // UI rebinds to the target runtime instantly; content follows.
    set({ currentSessionId: id });
    // Opening a session clears its unread cron badge.
    useCronUnreadStore.getState().markSeen(id);

    if (!entry.hydrated && !entry.hydrating) {
      entry.hydrating = (async () => {
        set({ isLoading: true });
        try {
          const session = await sessionsApi.getSession(id);
          const normalized = normalizeSession(session);
          set((state) => ({
            liveSessions: { ...state.liveSessions, [id]: normalized },
          }));
          // Bulk hydrate — one render per store, never addMessage loops.
          entry.chat.setState({ messages: normalized.chatHistory });
          entry.transcription.getState().loadSegments(
            normalized.segments,
            normalized.speakerNames ?? {},
          );
          entry.hydrated = true;
        } catch (err) {
          console.warn('Failed to load session:', err);
          useUIStore.getState().addToast({
            type: 'error',
            message: 'Failed to load session',
            duration: 4000,
          });
        } finally {
          set({ isLoading: false });
          entry.hydrating = null;
        }
      })();
    }
    await entry.hydrating;

    maybeEvictIdle();
  },

  deleteSession: async (id: string) => {
    await stopRecordingIfOwner(id);
    try {
      await sessionsApi.deleteSession(id);
    } catch (err) {
      console.warn('Failed to delete session:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to delete session on server (removed locally)',
        duration: 4000,
      });
    }

    // Tear down the runtime first: aborts any in-flight stream, closes
    // the event channel, cancels self-saving subscriptions.
    saveTimeouts.delete(id);
    dropRuntime(id);
    get().dropLiveSession(id);
    useIndexSearchStore.getState().clearSession(id);

    const wasActive = get().currentSessionId === id;
    const filtered = get().sessions.filter((s) => s.id !== id);

    if (!wasActive) {
      set({ sessions: filtered });
      return;
    }

    set({ sessions: filtered, currentSessionId: null });

    // Pick the next survivor (most-recent first since the list is sorted
    // by recency) and fully load it. If none remain, we stay on the
    // welcome / "new session" state and the user can start fresh.
    const next = filtered[0];
    if (next) {
      await get().switchSession(next.id);
    }
  },

  // Sidebar multi-select delete. One server round-trip, then the same
  // local cleanup as deleteSession, generalized over a set of ids.
  bulkDeleteSessions: async (ids: string[]) => {
    if (ids.length === 0) return;
    const toDelete = new Set(ids);
    for (const sid of toDelete) await stopRecordingIfOwner(sid);
    try {
      await sessionsApi.bulkDeleteSessions(ids);
    } catch (err) {
      console.warn('Failed to bulk-delete sessions:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to delete sessions on server (removed locally)',
        duration: 4000,
      });
    }

    for (const sid of toDelete) {
      saveTimeouts.delete(sid);
      dropRuntime(sid);
      get().dropLiveSession(sid);
      useIndexSearchStore.getState().clearSession(sid);
    }

    const wasActive = toDelete.has(get().currentSessionId ?? '');
    const filtered = get().sessions.filter((s) => !toDelete.has(s.id));

    if (!wasActive) {
      set({ sessions: filtered });
      return;
    }

    set({ sessions: filtered, currentSessionId: null });

    const next = filtered[0];
    if (next) {
      await get().switchSession(next.id);
    }
  },

  // Pin/archive toggles. Optimistic: the sidebar regroups instantly and the
  // PATCH lands in the background; failure rolls nothing back (next
  // loadSessions resyncs) but does surface a toast.
  setSessionFlags: (id: string, flags: { pinned?: boolean; archived?: boolean }) => {
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, ...flags } : s)),
    }));
    sessionsApi.setSessionFlags(id, flags).catch(() => {
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to update session on server',
        duration: 4000,
      });
    });
  },

  branchSession: async (id: string) => {
    try {
      // Flush-save first so branching a live (possibly mid-stream) session
      // copies its freshest persisted state. AWAIT the save: the backend
      // branch reads the persisted session, so it must land before we ask
      // the server to copy it — otherwise the branch inherits stale state.
      await get().saveSession(id);
      const res = await sessionsApi.branchSession(id);
      await get().loadSessions();
      await get().switchSession(res.new_session_id);
      useUIStore.getState().addToast({
        type: 'success',
        message: `Branched to "${res.name}"`,
        duration: 2500,
      });
    } catch (err) {
      console.warn('Failed to branch session:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to branch session',
        duration: 4000,
      });
    }
  },

  updateSessionTitle: (id: string, title: string, custom: boolean) => {
    set((state) => {
      const now = new Date().toISOString();
      const updated = state.sessions.map((s) =>
        s.id === id ? { ...s, title, date: now } : s,
      );
      updated.sort((a, b) => {
        if (!a.date && !b.date) return 0;
        if (!a.date) return 1;
        if (!b.date) return -1;
        return new Date(b.date).getTime() - new Date(a.date).getTime();
      });
      const live = state.liveSessions[id];
      return {
        sessions: updated,
        liveSessions: live
          ? {
              ...state.liveSessions,
              [id]: { ...live, title, customTitle: custom, generatedTitle: !custom, updatedAt: now },
            }
          : state.liveSessions,
      };
    });
    get().debouncedSave(id);
  },

  /**
   * Sync one session's chat messages into its live metadata + summary.
   * Called by the runtime registry whenever that session's stream ends —
   * foreground or background.
   */
  syncChatHistory: (id: string, messages: ChatMessage[]) => {
    const live = get().liveSessions[id];
    if (!live) return;

    set((state) => {
      const now = new Date().toISOString();
      const updated = state.sessions.map((s) =>
        s.id === id ? { ...s, chatCount: messages.length, date: now } : s,
      );
      updated.sort((a, b) => {
        if (!a.date && !b.date) return 0;
        if (!a.date) return 1;
        if (!b.date) return -1;
        return new Date(b.date).getTime() - new Date(a.date).getTime();
      });
      return {
        sessions: updated,
        liveSessions: {
          ...state.liveSessions,
          [id]: { ...live, chatHistory: messages, updatedAt: now },
        },
      };
    });

    get().debouncedSave(id);
  },

  saveSession: (id: string) => {
    const live = get().liveSessions[id];
    if (!live) return;

    // Compose from the session's OWN runtime stores — works identically
    // for foreground and background sessions.
    const chat = getChatStore(id).getState();
    const { segments, speakerNames } = getTranscriptionStore(id).getState();
    const sessionToSave = {
      ...live,
      chatHistory: chat.messages,
      segments,
      speakerNames,
      updatedAt: new Date().toISOString(),
    };
    return sessionsApi.updateSession(id, sessionToSave);
  },

  debouncedSave: (id: string) => {
    const existing = saveTimeouts.get(id);
    if (existing) clearTimeout(existing);
    saveTimeouts.set(
      id,
      setTimeout(() => {
        saveTimeouts.delete(id);
        get().saveSession(id);
      }, 2000),
    );
  },

  dropLiveSession: (id: string) => {
    set((state) => {
      if (!(id in state.liveSessions)) return state;
      const next = { ...state.liveSessions };
      delete next[id];
      return { liveSessions: next };
    });
  },

  clearChat: () => {
    const id = get().currentSessionId;
    if (!id) return;
    set((state) => {
      const live = state.liveSessions[id];
      if (!live) return state;
      return {
        liveSessions: { ...state.liveSessions, [id]: { ...live, chatHistory: [] } },
      };
    });
    get().debouncedSave(id);
  },
}), {
  name: 'whisper-session',
  storage: createJSONStorage(() => localStorage),
  // Only the id survives — live sessions, summaries, and loading state
  // are server-driven and must rehydrate fresh on mount.
  partialize: (state) => ({ currentSessionId: state.currentSessionId }),
}));
