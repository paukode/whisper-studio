/**
 * Workspace connection state shared by every control that changes it.
 *
 * Disconnect is optimistic: the UI lets go of the workspace the moment it is
 * clicked, and the POST follows. It used to wait for the POST first, with no
 * timeout and no feedback, so whenever the request could not be served at once
 * (queued behind the browser's six-per-origin connection cap while turns
 * stream, or behind a busy event loop) the button looked dead and a second
 * click queued a second request. The server does the same thing instantly and
 * never stops the running turn, so there is nothing to wait for.
 *
 * Three rules keep the optimistic state honest:
 *  - single flight: while one disconnect is in flight, another call returns it;
 *  - a connect waits for a pending disconnect (settleWorkspaceOps), so the
 *    server always sees them in the order the user made them;
 *  - a status answer requested before the workspace last changed is dropped
 *    (syncWorkspaceStatus), so a focus resync that was already in flight can
 *    not bring a disconnected workspace back on screen.
 * A failure is never swallowed: it is logged, shown as a toast, and the UI is
 * re-synced from /api/workspace/status, which is authoritative.
 */
import { post } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';

/** Budget for the disconnect request. The server answers in about a
 *  millisecond; this long means the request never reached it. */
export const DISCONNECT_TIMEOUT_MS = 10_000;

interface DisconnectResponse {
  disconnected?: boolean;
  /** Teardown steps that failed on the server; the disconnect itself held. */
  warnings?: string[];
}

let pendingDisconnect: Promise<void> | null = null;

/** Resolves once no disconnect is in flight. Every connect awaits it before
 *  sending its own POST, so a quick reconnect can never be overtaken by the
 *  disconnect that preceded it. */
export function settleWorkspaceOps(): Promise<void> {
  return pendingDisconnect ?? Promise.resolve();
}

/** Disconnect the workspace now. The UI updates synchronously; the returned
 *  promise settles when the server has answered (or the failure is shown). */
export function disconnectWorkspace(): Promise<void> {
  if (pendingDisconnect) return pendingDisconnect;
  useUIStore.getState().setWsConnected(false);
  useWorkspaceStore.getState().closeCleanTabs();
  pendingDisconnect = (async () => {
    const failure = await requestDisconnect();
    pendingDisconnect = null;
    if (failure) await reportFailure(failure);
  })();
  return pendingDisconnect;
}

async function requestDisconnect(): Promise<string | null> {
  const signal = AbortSignal.timeout(DISCONNECT_TIMEOUT_MS);
  try {
    const data = await post<DisconnectResponse>('/api/workspace/disconnect', undefined, { signal });
    for (const warning of data?.warnings ?? []) {
      console.warn('Workspace disconnect warning:', warning);
      useUIStore.getState().addToast({
        type: 'warning',
        message: `Workspace disconnected, but: ${warning}`,
        duration: 6000,
      });
    }
    return null;
  } catch (err) {
    console.warn('Workspace disconnect failed:', err);
    if (signal.aborted) {
      return `the server did not answer within ${DISCONNECT_TIMEOUT_MS / 1000} s`;
    }
    return err instanceof Error ? err.message : String(err);
  }
}

async function reportFailure(reason: string): Promise<void> {
  useUIStore.getState().addToast({
    type: 'error',
    message: `Could not disconnect the workspace: ${reason}`,
    duration: 6000,
  });
  try {
    await syncWorkspaceStatus();
  } catch (err) {
    // The server is unreachable: the focus resync in AppShell asks again.
    console.warn('Workspace status resync after a failed disconnect failed:', err);
  }
}

export type SyncOutcome = 'applied' | 'unchanged' | 'stale';

/**
 * Apply the server's answer to "which workspace is connected?".
 *
 * Dropped as stale when the workspace changed while the request was in
 * flight (the user's newer choice wins), or when a disconnect was pending at
 * either end of it (the server may have answered before it processed that
 * disconnect). Throws when the server cannot be reached or answers badly, so
 * the caller decides whether to retry.
 */
export async function syncWorkspaceStatus(): Promise<SyncOutcome> {
  const epoch = useUIStore.getState().wsEpoch;
  const startedDuringDisconnect = pendingDisconnect !== null;
  const response = await fetch('/api/workspace/status');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = (await response.json()) as { connected?: boolean; path?: string };
  const ui = useUIStore.getState();
  if (startedDuringDisconnect || pendingDisconnect !== null || ui.wsEpoch !== epoch) {
    return 'stale';
  }
  const path = data.connected && data.path ? data.path : '';
  if (path === ui.wsPath && Boolean(path) === ui.wsConnected) return 'unchanged';
  ui.setWsConnected(Boolean(path), path || undefined);
  return 'applied';
}
