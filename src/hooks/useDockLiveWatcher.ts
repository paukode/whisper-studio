import { useEffect } from 'react';
import { listPreviewSessions } from '@/api/preview';
import { useDockStore } from '@/stores/dockStore';

/** Poll cadence while a preview is live or the dock is open: the pane has to
 *  follow server switches promptly. */
export const ACTIVE_POLL_MS = 2000;
/** Poll cadence with no live preview and the dock closed. A fixed 2s poll here
 *  was 75% of a 221K-line backend log (two windows, ~a day) for a pane that
 *  was not even showing. */
export const IDLE_POLL_MS = 15000;

/**
 * useDockLiveWatcher — polls for the active preview session and feeds it to
 * dockStore. Mirrors Claude Code's single-preview model: the pane follows the
 * LATEST *alive* server, so spinning up a new server switches the pane to it,
 * and a server that has crashed (process_alive === false) is skipped rather
 * than shown as a dead iframe. The store handles auto-open, the persisted
 * "dismissed" flag, refreshing a stale panel, and the stopped state.
 *
 * Cadence adapts: fast while something is live or the dock is open, slow
 * otherwise, and paused entirely while the window is hidden (a poll that
 * nobody can see only fills the log). Coming back to the foreground polls at
 * once so the pane is current the moment it is looked at.
 */
export function useDockLiveWatcher(): void {
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // Set while a tick is awaiting the request: a visibilitychange landing
    // mid-tick must not start a second, parallel polling chain.
    let inFlight = false;

    const tick = async () => {
      let s: { name: string; url: string | null; port: number | null } | null = null;
      try {
        const list = await listPreviewSessions();
        // Latest-started session that isn't a crashed process (null = unknown,
        // e.g. a browser-only session, which we still allow).
        const usable = list.filter((x) => x.process_alive !== false);
        const active = usable[usable.length - 1];
        s = active ? { name: active.id, url: active.url, port: active.port } : null;
      } catch {
        s = null;
      }
      if (!alive) return;
      const dock = useDockStore.getState();
      const prev = dock.liveSession;
      const changed = prev?.name !== s?.name || prev?.url !== s?.url || prev?.port !== s?.port;
      if (changed) dock.setLiveSession(s);
    };

    const schedule = () => {
      if (!alive) return;
      const dock = useDockStore.getState();
      const busy = dock.liveSession !== null || dock.open;
      timer = setTimeout(loop, busy ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };

    const loop = async () => {
      timer = null;
      if (typeof document !== 'undefined' && document.hidden) {
        // Hidden: don't poll, don't reschedule. visibilitychange restarts us.
        return;
      }
      inFlight = true;
      try {
        await tick();
      } finally {
        inFlight = false;
      }
      schedule();
    };

    const onVisibility = () => {
      if (typeof document !== 'undefined' && !document.hidden && timer === null && !inFlight) {
        void loop();
      }
    };

    void loop();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      alive = false;
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);
}
