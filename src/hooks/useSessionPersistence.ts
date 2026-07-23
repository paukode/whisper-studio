import { useCallback, useEffect, useState } from 'react';
import { useSessionStore } from '@/stores/sessionStore';
import { getChatStore, getTranscriptionStore, useRuntimeIndex } from '@/stores/sessionRuntimes';

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
      const { liveSessions } = useSessionStore.getState();
      for (const id of useRuntimeIndex.getState().liveIds) {
        const live = liveSessions[id];
        if (!live) continue;
        // Fresh snapshot of every persisted surface from the session's
        // OWN stores at flush time — in-flight chat and the last
        // un-debounced transcript segments included.
        const { segments, speakerNames } = getTranscriptionStore(id).getState();
        const payload = {
          ...live,
          chatHistory: getChatStore(id).getState().messages,
          segments,
          speakerNames,
          updatedAt: new Date().toISOString(),
        };
        navigator.sendBeacon(
          `/api/sessions/${encodeURIComponent(id)}/beacon`,
          new Blob([JSON.stringify(payload)], { type: 'application/json' }),
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
