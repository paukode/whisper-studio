import { useEffect } from 'react';
import { SocketEventSource } from '@/api/streamSocket';

/**
 * Single shared subscription to the global `/api/git/events` SSE.
 *
 * The status bar, the Git Changes panel, and the terminal panel all react to
 * the same global `git-changed` frame through ONE ref-counted stream
 * (SocketEventSource, over a WebSocket), fanned out to every listener
 * in-process.
 *
 * Each consumer still keeps its own debounce and refetch, so the fan-out only
 * replaces the transport, not the per-consumer behaviour.
 */
type GitChangedListener = () => void;

const listeners = new Set<GitChangedListener>();
let source: SocketEventSource | null = null;

function openSource(): void {
  if (source || typeof EventSource === 'undefined') return; // vitest/jsdom
  source = new SocketEventSource('/api/git/events');
  source.onmessage = (e) => {
    try {
      const parsed = JSON.parse(e.data) as { type?: string };
      // Only `git-changed` is actionable; `no-workspace`, keep-alive comment
      // lines, and malformed frames are ignored.
      if (parsed.type === 'git-changed') {
        for (const cb of listeners) cb();
      }
    } catch {
      /* keep-alive comment or malformed frame */
    }
  };
  // The stream reconnects on a transient drop and the backend re-emits an
  // immediate `git-changed` on reconnect, so no onerror handling is needed.
}

function closeSource(): void {
  source?.close();
  source = null;
}

/** Register a git-changed listener against the shared connection. Opens the
 *  connection on the first subscriber and closes it when the last leaves.
 *  Returns an unsubscribe function. */
function subscribeGitChanged(cb: GitChangedListener): () => void {
  listeners.add(cb);
  openSource();
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) closeSource();
  };
}

/**
 * Subscribe a component to global git-changed events for its lifetime.
 *
 * @param onChanged stable callback (wrap in useCallback) run on each change.
 * @param active    when false the component does not subscribe (e.g. the
 *                   status bar with no workspace, or a collapsed terminal).
 */
export function useGitChanged(onChanged: () => void, active = true): void {
  useEffect(() => {
    if (!active) return;
    return subscribeGitChanged(onChanged);
  }, [onChanged, active]);
}
