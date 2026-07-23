import { useCallback } from 'react';
import { useUIStore } from '@/stores/uiStore';

export interface SaveToastMessages {
  success?: string;
  /** Prefix for the error toast; the server's reason is appended when present. */
  error?: string;
}

/**
 * Toast-based sibling of {@link useSaveStatus} for save actions whose inline
 * indicator would unmount on success (e.g. an editor that closes after saving,
 * or a list row that refetches). Runs the async op and confirms the outcome
 * with a success / error toast, swallowing the rejection so callers get
 * feedback without their own try/catch. Resolves `true` on success.
 */
export function useSaveToast() {
  const addToast = useUIStore((s) => s.addToast);
  return useCallback(
    async (fn: () => Promise<unknown>, messages?: SaveToastMessages): Promise<boolean> => {
      try {
        await fn();
        addToast({ type: 'success', message: messages?.success ?? 'Saved' });
        return true;
      } catch (err) {
        const reason = err instanceof Error && err.message ? err.message : '';
        const base = messages?.error ?? 'Save failed';
        addToast({ type: 'error', message: reason ? `${base}: ${reason}` : base });
        console.warn('[useSaveToast]', base, err);
        return false;
      }
    },
    [addToast],
  );
}
