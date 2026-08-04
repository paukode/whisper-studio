import { post } from '@/api/client';
import { queryClient } from '@/providers/QueryProvider';

export interface PersistedToast {
  /** Origin slug shown as the badge in the bell (e.g. "app", "chat", "model-download"). */
  source?: string;
  title?: string;
  message: string;
  status: 'success' | 'error' | 'warning' | 'info';
}

/**
 * Durable copy of a user-facing toast: POSTs it to the persistent
 * notification log (server/notifications.py) so the header bell keeps the
 * full history after the toast auto-dismisses. Wired centrally in the
 * uiStore's addToast — call sites opt OUT with `persist: false` for
 * inherently transient feedback (progress ticks, "copied", usage hints).
 *
 * Fire-and-forget by design: the toast has already been shown, so a failed
 * write must never surface a second error (which could itself recurse into
 * addToast). The server merges identical bursts into one row with a repeat
 * count, so tight loops don't need client-side throttling.
 */
export function persistToastNotification(t: PersistedToast): void {
  try {
    void post('/api/notifications', {
      source: t.source ?? 'app',
      title: t.title ?? '',
      message: t.message,
      status: t.status,
    })
      .then(() => {
        // Refresh the bell badge promptly instead of waiting out its poll.
        void queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
        void queryClient.invalidateQueries({ queryKey: ['notifications-list'] });
      })
      .catch(() => {
        /* best-effort */
      });
  } catch {
    /* fetch may not exist in exotic embeds; the toast still showed */
  }
}
