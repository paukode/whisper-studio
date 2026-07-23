/**
 * Session runtime registry — the core of parallel sessions.
 *
 * Each live session owns a RuntimeEntry: its own chat store, its own
 * transcription store, its own cron EventSource, its own in-flight chat
 * AbortController, and self-saving subscriptions. The UI binds to the
 * ACTIVE session's stores through the wrapper hooks below and simply
 * re-points on switch; streams and recordings bind to their OWNING
 * session's stores at start time and never notice switches at all.
 *
 * Nothing is cleared on switch. Background sessions keep streaming,
 * keep receiving cron events, and keep saving themselves. The registry
 * is soft-capped: beyond MAX_LIVE_RUNTIMES, idle hydrated sessions are
 * evicted LRU (never the current one, never one that is streaming,
 * recording, mid-approval, or has an unflushed save).
 */
import { create, useStore } from 'zustand';
import type { StoreApi } from 'zustand/vanilla';
import { createChatStore, type ChatState } from './chatStore';
import { createTranscriptionStore, type TranscriptionState } from './transcriptionStore';
import { useSessionStore } from './sessionStore';
import { useRecordingStore } from './recordingStore';
import { useCronUnreadStore } from './cronUnreadStore';
import type {
  ChatMessage,
  CronEventPayload,
  TaskEventPayload,
  TeamProgressEvent,
} from '@/types/chat';

/** Backs `currentSessionId === null` (welcome screen, pre-first-session).
 *  Never saved, never evicted, never event-sourced. */
export const DRAFT_SESSION = '__draft__';

/** How many session runtimes stay in memory (the user-facing "3 active
 *  sessions"). The cap is soft: busy sessions are never evicted, so the
 *  registry may transiently exceed it until one goes idle. */
export const MAX_LIVE_RUNTIMES = 3;

export interface RuntimeEntry {
  chat: StoreApi<ChatState>;
  transcription: StoreApi<TranscriptionState>;
  /** Long-lived cron/notification SSE for this session. */
  events: EventSource | null;
  /** In-flight /api/chat stream for this session, if any. */
  abort: AbortController | null;
  unsubs: Array<() => void>;
  lastUsed: number;
  /** Whether the stores hold the server's copy of this session. */
  hydrated: boolean;
  /** Dedupes concurrent hydration (double-switch races). */
  hydrating: Promise<void> | null;
}

const runtimes = new Map<string, RuntimeEntry>();

/** Reactive index of live (non-draft) runtime ids, so React surfaces
 *  (sidebar badges, persistence loops) re-render as the set changes. */
export const useRuntimeIndex = create<{ liveIds: string[] }>(() => ({ liveIds: [] }));

function syncIndex(): void {
  useRuntimeIndex.setState({
    liveIds: [...runtimes.keys()].filter((k) => k !== DRAFT_SESSION),
  });
}

export function getRuntime(sessionId: string | null): RuntimeEntry {
  const key = sessionId ?? DRAFT_SESSION;
  let entry = runtimes.get(key);
  if (!entry) {
    entry = {
      chat: createChatStore(),
      transcription: createTranscriptionStore(),
      events: null,
      abort: null,
      unsubs: [],
      lastUsed: Date.now(),
      hydrated: key === DRAFT_SESSION,
      hydrating: null,
    };
    runtimes.set(key, entry);
    if (key !== DRAFT_SESSION) {
      attachRuntimeSubscriptions(key, entry);
      openEventStream(key, entry);
      syncIndex();
    }
  }
  return entry;
}

export function hasRuntime(sessionId: string): boolean {
  return runtimes.has(sessionId);
}

export const getChatStore = (sessionId: string | null): StoreApi<ChatState> =>
  getRuntime(sessionId).chat;

export const getTranscriptionStore = (sessionId: string | null): StoreApi<TranscriptionState> =>
  getRuntime(sessionId).transcription;

// ── Active-session bindings ────────────────────────────────────────────
// Hooks re-subscribe when currentSessionId changes (useStore is built on
// useSyncExternalStore: a new store identity swaps subscription and
// snapshot in one commit). The imperative getters resolve the active
// session AT CALL TIME — correct for user-event handlers, and exactly
// wrong for stream pipelines, which must bind getChatStore(sid) once at
// send time instead.

export function useActiveChatStore<T>(selector: (s: ChatState) => T): T {
  const sid = useSessionStore((s) => s.currentSessionId);
  return useStore(getChatStore(sid), selector);
}

export const getActiveChatStore = (): StoreApi<ChatState> =>
  getChatStore(useSessionStore.getState().currentSessionId);

export function useActiveTranscriptionStore<T>(selector: (s: TranscriptionState) => T): T {
  const sid = useSessionStore((s) => s.currentSessionId);
  return useStore(getTranscriptionStore(sid), selector);
}

export const getActiveTranscriptionStore = (): StoreApi<TranscriptionState> =>
  getTranscriptionStore(useSessionStore.getState().currentSessionId);

// ── Activity ───────────────────────────────────────────────────────────

export type SessionActivity = 'streaming' | 'approval' | 'recording' | null;

export function activityFor(sessionId: string): SessionActivity {
  if (useRecordingStore.getState().recordingSessionId === sessionId) return 'recording';
  const entry = runtimes.get(sessionId);
  if (!entry) return null;
  const chat = entry.chat.getState();
  if (chat.currentApproval !== null) return 'approval';
  if (chat.isStreaming) return 'streaming';
  return null;
}

/** Reactive activity state for one session row (sidebar badges). */
export function useSessionActivity(sessionId: string): SessionActivity {
  // liveIds + recordingSessionId changes re-render the row; the chat
  // store of a live session is subscribed directly when present.
  const live = useRuntimeIndex((s) => s.liveIds.includes(sessionId));
  const recording = useRecordingStore((s) => s.recordingSessionId === sessionId);
  const entry = live ? runtimes.get(sessionId) : undefined;
  const draft = getRuntime(DRAFT_SESSION); // stable fallback store, always idle
  const chatActivity = useStore(
    (entry ?? draft).chat,
    (s) => (s.currentApproval !== null ? 'approval' : s.isStreaming ? 'streaming' : null),
  );
  if (recording) return 'recording';
  if (!live) return null;
  return chatActivity as SessionActivity;
}

/** ACTIVE = streaming, mid-approval, or owning the recording. The user's
 *  3-session ceiling counts these; viewing idle sessions is never capped. */
export function countActiveSessions(excluding?: string): number {
  const recOwner = useRecordingStore.getState().recordingSessionId;
  let count = 0;
  for (const [sid, entry] of runtimes) {
    if (sid === DRAFT_SESSION || sid === excluding) continue;
    const chat = entry.chat.getState();
    if (chat.isStreaming || chat.currentApproval !== null || sid === recOwner) count++;
  }
  return count;
}

// ── Self-saving subscriptions ──────────────────────────────────────────
// These replace the old AppShell singleton subscriptions: every runtime
// saves itself on change, foreground or background, so nothing is lost
// when the user switches away mid-anything.

function attachRuntimeSubscriptions(sid: string, entry: RuntimeEntry): void {
  let wasStreaming = entry.chat.getState().isStreaming;
  let prevMsgCount = entry.chat.getState().messages.length;
  entry.unsubs.push(
    entry.chat.subscribe((s) => {
      // Stream end → full sync (counts, sort, debounced save).
      if (wasStreaming && !s.isStreaming && s.messages.length > 0) {
        setTimeout(() => {
          if (!runtimes.has(sid)) return; // evicted/deleted meanwhile
          useSessionStore.getState().syncChatHistory(sid, entry.chat.getState().messages);
        }, 200);
      }
      wasStreaming = s.isStreaming;
      // Any message-count change (user question lands immediately, cron
      // events, edits/deletes) → background save soon after.
      if (s.messages.length !== prevMsgCount) {
        prevMsgCount = s.messages.length;
        useSessionStore.getState().debouncedSave(sid);
      }
    }),
  );

  let prevSegLen = entry.transcription.getState().segments.length;
  let prevSpeakers = entry.transcription.getState().speakerNames;
  entry.unsubs.push(
    entry.transcription.subscribe((s) => {
      if (s.segments.length !== prevSegLen || s.speakerNames !== prevSpeakers) {
        prevSegLen = s.segments.length;
        prevSpeakers = s.speakerNames;
        useSessionStore.getState().debouncedSave(sid);
      }
    }),
  );
}

// ── Per-session cron/notification SSE ──────────────────────────────────
// Replaces useSessionEventStream (active-session-only): a background live
// session must receive its cron events too, since non-destructive
// switching skips the rehydrate that used to replay them.

/** Memory activity payload forwarded by /api/sessions/{id}/events. */
interface MemoryEventPayload {
  action: 'recalled' | 'extracted';
  count?: number;
  writes?: number;
  deletes?: number;
}

/** Quiet toast for background memory activity (recall happens pre-stream and
 *  extraction after the chat SSE closes, so this channel is their only path
 *  to the UI). Keyed so rapid events collapse instead of stacking. */
function toastMemoryEvent(sid: string, ev: MemoryEventPayload): void {
  if (sid !== useSessionStore.getState().currentSessionId) return; // background session: stay quiet
  let message: string;
  let key: string;
  if (ev.action === 'recalled') {
    const n = ev.count ?? 0;
    message = n === 1 ? 'Recalled 1 memory' : `Recalled ${n} memories`;
    key = `memory-recalled-${n}`;
  } else {
    const w = ev.writes ?? 0;
    const d = ev.deletes ?? 0;
    const parts: string[] = [];
    if (w) parts.push(w === 1 ? '1 memory saved' : `${w} memories saved`);
    if (d) parts.push(`${d} removed`);
    if (!parts.length) return;
    message = `Memory updated: ${parts.join(', ')}`;
    key = `memory-extracted-${w}-${d}`;
  }
  // The dedup key carries the counts: addToast's collapse path bumps a
  // counter but keeps the FIRST message, so "Recalled 2" followed by
  // "Recalled 5" must not merge into a stale "Recalled 2 ×2".
  // Lazy import avoids a cycle (uiStore has no dependency back on runtimes,
  // but keep the import local to the event path anyway).
  void import('./uiStore').then(({ useUIStore }) => {
    useUIStore.getState().addToast({ type: 'info', message, key });
  });
}

function openEventStream(sid: string, entry: RuntimeEntry): void {
  if (typeof EventSource === 'undefined') return; // vitest/jsdom
  const es = new EventSource(`/api/sessions/${encodeURIComponent(sid)}/events`);
  es.onmessage = (event) => {
    if (!event.data) return;
    let parsed: {
      cron_event?: CronEventPayload;
      memory_event?: MemoryEventPayload;
      task_event?: TaskEventPayload;
      team_progress?: TeamProgressEvent;
      ci_progress?: Record<string, unknown>;
      ci_result?: Record<string, unknown>;
    } | null = null;
    try {
      parsed = JSON.parse(event.data) as {
        cron_event?: CronEventPayload;
        memory_event?: MemoryEventPayload;
        task_event?: TaskEventPayload;
        team_progress?: TeamProgressEvent;
        ci_progress?: Record<string, unknown>;
        ci_result?: Record<string, unknown>;
      };
    } catch {
      return; // heartbeats / malformed frames
    }
    if (parsed?.ci_progress) {
      const ev = parsed.ci_progress;
      const taskId = String(ev.task_id ?? '');
      if (taskId) {
        void import('./ciStore').then(({ useCIStore }) =>
          useCIStore.getState().applyProgress(taskId, ev),
        );
      }
      return;
    }
    if (parsed?.ci_result) {
      const payload = parsed.ci_result;
      const taskId = String(payload.task_id ?? '');
      if (taskId) {
        void import('./ciStore').then(({ useCIStore }) =>
          useCIStore.getState().applyResult(taskId, payload),
        );
      }
      return;
    }
    if (parsed?.team_progress) {
      // Live cron-run frames (agent_id "cron:<run_id>") arrive on this
      // long-lived stream; fold them with the exact logic the chat SSE uses
      // so a running cron job renders as a live agent card.
      entry.chat.getState().foldTeamEvent(parsed.team_progress);
      return;
    }
    if (parsed?.memory_event) {
      toastMemoryEvent(sid, parsed.memory_event);
      return;
    }
    if (parsed?.task_event) {
      const payload = parsed.task_event;
      const msg: ChatMessage = {
        role: 'task_event',
        content: '',
        timestamp: payload.timestamp ?? new Date().toISOString(),
        taskEvent: payload,
      };
      entry.chat.getState().addMessage(msg);
      // Keep the global running-count pill live. Lazy import mirrors the
      // uiStore pattern above (no cycle, but stay consistent).
      void import('./backgroundTaskStore').then(({ useBackgroundTaskStore }) => {
        useBackgroundTaskStore.getState().applyEvent(sid, payload);
      });
      return;
    }
    if (parsed?.cron_event) {
      const payload = parsed.cron_event;
      const msg: ChatMessage = {
        role: 'cron_event',
        content: '',
        timestamp: payload.timestamp ?? new Date().toISOString(),
        cronEvent: payload,
      };
      entry.chat.getState().addMessage(msg);

      // A firing in a background session raises an unread badge on its
      // sidebar row (the poll in useCronUnreadSync is the authoritative
      // backstop; this makes the badge appear instantly).
      if (payload.event_type === 'cron_fired') {
        const activeSid = useSessionStore.getState().currentSessionId;
        if (sid !== activeSid) {
          useCronUnreadStore.getState().bumpLive({
            run_id: payload.run_id ?? payload.timestamp ?? `${sid}-${Date.now()}`,
            job_id: payload.cron_id,
            job_name: payload.cron_name,
            session_id: sid,
            status: payload.status ?? 'ok',
            started_at: payload.timestamp ?? new Date().toISOString(),
          });
        }
      }
    }
  };
  // Browser auto-reconnects EventSource on transient errors; missed events are
  // durable in chat_history and replay on the next hydrate. On a hard CLOSED
  // (server gone, or the socket was killed) drop our reference so the stream is
  // recreated on the next recycle instead of lingering as a dead connection
  // that still holds one of the browser's ~6 per-host slots.
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && runtimes.get(sid) === entry) {
      entry.events = null;
    }
  };
  entry.events = es;
}

/** Close and reopen stale cron streams. After a laptop wake, tab refocus, or
 *  network restore, EventSources can be left half-open (Chrome:
 *  ERR_NETWORK_IO_SUSPENDED) — still holding one of the browser's ~6 per-host
 *  connection slots without delivering events. Enough leaked slots starve new
 *  /api/chat requests, which looks like "sessions stopped working." `force`
 *  recycles every stream (used on `online`, which means we were definitely
 *  offline); otherwise only non-OPEN streams are recycled so a routine tab
 *  refocus doesn't churn healthy connections. */
function recycleEventStreams(force: boolean): void {
  for (const [sid, entry] of runtimes) {
    if (sid === DRAFT_SESSION) continue;
    const es = entry.events;
    if (!force && es && es.readyState === EventSource.OPEN) continue;
    if (es) { try { es.close(); } catch { /* already closed */ } }
    entry.events = null;
    openEventStream(sid, entry);
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', () => recycleEventStreams(true));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') recycleEventStreams(false);
  });
}

// ── Teardown + eviction ────────────────────────────────────────────────

/** Remove a runtime (session deleted, or evicted after a flush-save).
 *  Aborts any in-flight stream and closes the event channel. */
export function dropRuntime(sessionId: string): void {
  const entry = runtimes.get(sessionId);
  if (!entry) return;
  entry.abort?.abort();
  entry.events?.close();
  for (const unsub of entry.unsubs) unsub();
  runtimes.delete(sessionId);
  syncIndex();
}

function isEvictable(sid: string, entry: RuntimeEntry): boolean {
  if (sid === DRAFT_SESSION) return false;
  if (sid === useSessionStore.getState().currentSessionId) return false;
  if (sid === useRecordingStore.getState().recordingSessionId) return false;
  const chat = entry.chat.getState();
  if (chat.isStreaming || chat.currentApproval !== null) return false;
  return entry.hydrated && entry.hydrating === null;
}

/** LRU-evict idle runtimes beyond the cap. Flushes a save first so the
 *  evicted session's latest state is durable before its stores go away. */
export function maybeEvictIdle(): void {
  const live = [...runtimes.entries()].filter(([k]) => k !== DRAFT_SESSION);
  if (live.length <= MAX_LIVE_RUNTIMES) return;
  const candidates = live
    .filter(([sid, entry]) => isEvictable(sid, entry))
    .sort((a, b) => a[1].lastUsed - b[1].lastUsed);
  let excess = live.length - MAX_LIVE_RUNTIMES;
  for (const [sid] of candidates) {
    if (excess <= 0) break;
    useSessionStore.getState().saveSession(sid);
    dropRuntime(sid);
    useSessionStore.getState().dropLiveSession(sid);
    excess--;
  }
}
