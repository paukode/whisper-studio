import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Shared "save feedback" state machine for settings actions.
 *
 * Wrap any persist call (PUT/POST/DELETE) in `run()` and the hook drives a
 * small status through saving → saved / error, auto-clearing after a delay.
 * Render the result with the <SaveStatus> component so every save button in
 * the app reports success/failure the same way.
 */
export type SaveState = 'idle' | 'saving' | 'saved' | 'error';

export interface SaveMessages {
  saving?: string;
  saved?: string;
  error?: string;
}

export interface SaveStatusHandle {
  state: SaveState;
  message: string;
  /**
   * Run an async save. Drives state to `saving`, then `saved` or `error`, and
   * auto-clears back to idle. Errors are swallowed (and logged) so callers get
   * feedback for free without their own try/catch. Resolves to `true` on
   * success, `false` on failure.
   */
  run: (fn: () => Promise<unknown>, messages?: SaveMessages) => Promise<boolean>;
  /** Clear back to idle immediately (e.g. when the user edits again). */
  reset: () => void;
}

const DEFAULTS: Required<SaveMessages> = {
  saving: 'Saving…',
  saved: 'Saved',
  error: 'Save failed',
};

export function useSaveStatus(clearAfterMs = 3000): SaveStatusHandle {
  const [state, setState] = useState<SaveState>('idle');
  const [message, setMessage] = useState('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const reset = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setState('idle');
    setMessage('');
  }, []);

  const run = useCallback(
    async (fn: () => Promise<unknown>, messages?: SaveMessages): Promise<boolean> => {
      const msg = { ...DEFAULTS, ...messages };
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setState('saving');
      setMessage(msg.saving);

      let ok = false;
      try {
        await fn();
        ok = true;
      } catch (err) {
        if (mountedRef.current) {
          setState('error');
          // Surface the server's reason when there is one — the API client's
          // ApiError carries a clean, human-readable message.
          const reason = err instanceof Error && err.message ? err.message : '';
          setMessage(reason ? `${msg.error}: ${reason}` : msg.error);
        }
        console.warn('[useSaveStatus] save failed:', err);
      }

      // Bail if the component unmounted mid-save (modal closed) — nothing to
      // update and no timer worth scheduling.
      if (!mountedRef.current) return ok;

      if (ok) {
        setState('saved');
        setMessage(msg.saved);
      }
      if (clearAfterMs > 0) {
        timerRef.current = setTimeout(() => {
          if (!mountedRef.current) return;
          setState('idle');
          setMessage('');
        }, clearAfterMs);
      }
      return ok;
    },
    [clearAfterMs],
  );

  return { state, message, run, reset };
}
