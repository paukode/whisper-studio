import { useCallback, useEffect, useState } from 'react';
import { sessionSaveBody, useSessionStore } from '@/stores/sessionStore';
import { useRuntimeIndex } from '@/stores/sessionRuntimes';

export interface UseSessionPersistenceReturn {
  save: () => void;
  isSaving: boolean;
}

/**
 * Background durability for EVERY live session, not just the visible one.
 *
 * Per-change saves are owned by the runtime registry (each session's
 * stores save themselves on mutation). This hook adds the two safety
 * nets that have to span all live runtimes:
 *   - a 30s periodic save loop, so SQLite stays fresh on idle tabs;
 *   - a `beforeunload` beacon PER LIVE SESSION, so a tab close mid-stream
 *     or mid-recording loses nothing in any session (the server's
 *     per-session locks serialize the concurrent writes).
 * Neither sends a session the server already holds as is (saveSession skips
 * it, the beacon checks isSaved), so an open session nobody touches costs
 * nothing; a failed save is not acknowledged and goes out again on the next
 * tick or in the beacon.
 */
export function useSessionPersistence(): UseSessionPersistenceReturn {
  const [isSaving, setIsSaving] = useState(false);

  const save = useCallback(() => {
    setIsSaving(true);
    try {
      const { liveSessions, saveSession } = useSessionStore.getState();
      for (const id of Object.keys(liveSessions)) saveSession(id);
    } finally {
      setIsSaving(false);
    }
  }, []);

  useEffect(() => {
    const handleBeforeUnload = () => {
      const { isSaved } = useSessionStore.getState();
      for (const id of useRuntimeIndex.getState().liveIds) {
        if (isSaved(id)) continue;
        // Fresh snapshot of every persisted surface from the session's
        // OWN stores at flush time: in-flight chat and the last
        // un-debounced transcript segments included. Same body as
        // saveSession, so both follow the same server rules.
        const body = sessionSaveBody(id);
        if (!body) continue;
        navigator.sendBeacon(
          `/api/sessions/${encodeURIComponent(id)}/beacon`,
          new Blob([JSON.stringify(body)], { type: 'application/json' }),
        );
      }
    };

    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, []);

  // Periodic save every 30s across all live sessions.
  useEffect(() => {
    const interval = setInterval(save, 30_000);
    return () => clearInterval(interval);
  }, [save]);

  return { save, isSaving };
}
