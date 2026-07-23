import { useEffect } from 'react';
import { listPreviewSessions } from '@/api/preview';
import { useDockStore } from '@/stores/dockStore';

const POLL_MS = 2000;

/**
 * useDockLiveWatcher — polls for the active preview session and feeds it to
 * dockStore. Mirrors Claude Code's single-preview model: the pane follows the
 * LATEST *alive* server, so spinning up a new server switches the pane to it,
 * and a server that has crashed (process_alive === false) is skipped rather
 * than shown as a dead iframe. The store handles auto-open, the persisted
 * "dismissed" flag, refreshing a stale panel, and the stopped state.
 */
export function useDockLiveWatcher(): void {
  useEffect(() => {
    let alive = true;
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
    void tick();
    const iv = setInterval(tick, POLL_MS);
    return () => { alive = false; clearInterval(iv); };
  }, []);
}
