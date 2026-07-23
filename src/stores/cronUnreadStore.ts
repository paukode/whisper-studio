/**
 * Unread cron-run badges, keyed by session_id.
 *
 * A cron firing lands in its owning session's chat even when that session
 * isn't open (or has no live runtime). This store tracks which runs the user
 * hasn't seen so the sidebar can badge those sessions.
 *
 * Two feeds, one source of truth:
 *   - Authoritative: GET /api/cron/runs/recent, polled by useCronUnreadSync,
 *     merged in (survives runtime eviction — works for any session).
 *   - Immediate: sessionRuntimes' live SSE handler calls bumpLive() so the
 *     badge appears the instant a background firing arrives.
 *
 * "Seen" is per-session and persisted: markSeen(sid) (called on session focus)
 * stamps now; unread = runs for that session newer than its last-seen stamp.
 */
import { useMemo } from 'react';
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type { CronRecentRun } from '@/api/cron';

const MAX_RUNS = 300;

function ts(run: CronRecentRun): number {
  const t = Date.parse(run.started_at);
  return Number.isNaN(t) ? 0 : t;
}

interface CronUnreadState {
  /** session_id -> epoch ms of last time the user looked at it. */
  lastSeen: Record<string, number>;
  /** Recent runs across all jobs, newest first. */
  runs: CronRecentRun[];
  /** Replace/merge the authoritative feed (dedupe by run_id, keep newest). */
  setRuns: (runs: CronRecentRun[]) => void;
  /** Record a single run the instant it arrives over SSE. */
  bumpLive: (run: CronRecentRun) => void;
  /** Mark a session's cron runs as seen (clears its badge). */
  markSeen: (sessionId: string) => void;
}

function mergeRuns(existing: CronRecentRun[], incoming: CronRecentRun[]): CronRecentRun[] {
  const byId = new Map<string, CronRecentRun>();
  for (const r of existing) byId.set(r.run_id, r);
  for (const r of incoming) byId.set(r.run_id, r);
  return [...byId.values()].sort((a, b) => ts(b) - ts(a)).slice(0, MAX_RUNS);
}

export const useCronUnreadStore = create<CronUnreadState>()(
  persist(
    (set) => ({
      lastSeen: {},
      runs: [],
      setRuns: (runs) => set((s) => ({ runs: mergeRuns(s.runs, runs) })),
      bumpLive: (run) => set((s) => ({ runs: mergeRuns(s.runs, [run]) })),
      markSeen: (sessionId) =>
        set((s) => ({ lastSeen: { ...s.lastSeen, [sessionId]: Date.now() } })),
    }),
    {
      name: 'whisper-cron-unread',
      storage: createJSONStorage(() => localStorage),
      // Only the seen-stamps persist; runs are refetched on mount.
      partialize: (state) => ({ lastSeen: state.lastSeen }),
    },
  ),
);

export interface CronUnread {
  count: number;
  hasFailure: boolean;
}

/** Unread summary for one session. Reactive: re-renders when runs or the
 *  session's last-seen stamp change.
 *
 *  The selectors return only STABLE references (the runs array, a number) —
 *  never a fresh object — because under zustand v5 a selector that builds a new
 *  object/array each call breaks useSyncExternalStore snapshot caching and
 *  triggers an infinite render loop (React error #185). The derived summary is
 *  computed in a useMemo instead. */
export function useCronUnreadFor(sessionId: string): CronUnread {
  const runs = useCronUnreadStore((s) => s.runs);
  const seen = useCronUnreadStore((s) => s.lastSeen[sessionId] ?? 0);
  return useMemo(() => {
    let count = 0;
    let hasFailure = false;
    for (const r of runs) {
      if (r.session_id !== sessionId) continue;
      if (ts(r) <= seen) continue;
      count += 1;
      if (r.status === 'failed') hasFailure = true;
    }
    return { count, hasFailure };
  }, [runs, seen, sessionId]);
}
