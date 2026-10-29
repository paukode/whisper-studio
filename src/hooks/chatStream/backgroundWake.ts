/**
 * Start a turn when background commands a session started have finished.
 *
 * The server keeps each result as a `task_report` row and sends a
 * `task_wake` frame when no turn was running to read it (server/tasks/wake.py).
 * This window then starts an ordinary chat turn for that session: it streams
 * into the chat, follows the permission mode and shows approval cards like
 * any other turn. The request carries no words of the user's; the server
 * words it.
 *
 * A session that is busy (streaming, an approval card waiting, an approved
 * action still running) wakes once it is free, and one that would pass the
 * active-session ceiling waits for a slot: sessionRuntimes calls
 * flushPendingWakes whenever a stream ends. Several results that land before
 * the wake starts share one turn. A session with no hydrated runtime in this
 * window does not wake; its rows wait for the next message.
 *
 * useChatStream loads lazily: it imports the session runtimes, which load
 * this module, so a static import would close that loop.
 */
import {
  countActiveSessions,
  hasHydratedRuntime,
  isSessionBusy,
} from '@/stores/sessionRuntimes';

/** Finished task ids per session, waiting for that session's wake turn. */
const pending = new Map<string, string[]>();

/** Note finished commands for `sessionId` and start its wake turn if it can
 *  run now. */
export function requestWake(sessionId: string, taskIds: string[]): void {
  if (!sessionId || !hasHydratedRuntime(sessionId)) return;
  const ids = pending.get(sessionId) ?? [];
  for (const id of taskIds) if (id && !ids.includes(id)) ids.push(id);
  pending.set(sessionId, ids);
  tryWake(sessionId);
}

/** Start every waiting wake that can run now. */
export function flushPendingWakes(): void {
  for (const sessionId of [...pending.keys()]) tryWake(sessionId);
}

function tryWake(sessionId: string): void {
  const ids = pending.get(sessionId);
  if (!ids) return;
  if (!hasHydratedRuntime(sessionId)) {
    // Evicted or deleted meanwhile: its rows wait for the next message.
    pending.delete(sessionId);
    return;
  }
  if (isSessionBusy(sessionId)) return;
  void import('../useChatStream').then(({ MAX_ACTIVE_SESSIONS, sendChatTurn }) => {
    // Read again: another flush may have started it, or the session may
    // have become busy, while the module loaded.
    const waiting = pending.get(sessionId);
    if (!waiting || !hasHydratedRuntime(sessionId) || isSessionBusy(sessionId)) return;
    if (countActiveSessions(sessionId) >= MAX_ACTIVE_SESSIONS) return;
    pending.delete(sessionId);
    void sendChatTurn('', {
      sessionId,
      hideUserMessage: true,
      runtimeWake: { task_ids: waiting },
    });
  });
}

/** Test seam: forget every waiting wake. */
export function _resetPendingWakes(): void {
  pending.clear();
}
