/**
 * Stream lifecycle control — the abort-controller registry and the
 * instant kill switch.
 *
 * Lives in its own leaf module (not useChatStream) because BOTH
 * useChatStream's send() and sseStream's sendApprovalContinuation()
 * register controllers here, and useChatStream already imports
 * sseStream — a shared leaf keeps the import graph acyclic.
 */
import { getChatStore, getRuntime } from '@/stores/sessionRuntimes';
import { useSubagentStore } from '@/stores/subagentStore';
import type { ChatMessage } from '@/types/chat';

/** One in-flight controller per session, module-scoped so it survives
 *  component remounts and session switches. */
const abortControllers = new Map<string, AbortController>();

/** Controllers whose stream UI was already finalized by killSessionStream.
 *  Keyed by CONTROLLER identity (not session id): a later stream in the
 *  same session gets a fresh controller and is unaffected, and a WeakSet
 *  needs no reset bookkeeping between sends. */
const killFinalized = new WeakSet<AbortController>();

/** Track a stream's controller for the session (send + continuations). */
export function registerStreamController(
  sessionId: string,
  controller: AbortController,
): void {
  abortControllers.set(sessionId, controller);
  getRuntime(sessionId).abort = controller;
}

/** Drop the controller — but only if it is still the one registered, so a
 *  re-send that already replaced it is never clobbered by the old finally. */
export function releaseStreamController(
  sessionId: string,
  controller: AbortController,
): void {
  if (abortControllers.get(sessionId) === controller) {
    abortControllers.delete(sessionId);
  }
  const runtime = getRuntime(sessionId);
  if (runtime.abort === controller) runtime.abort = null;
}

/** Whether killSessionStream already finalized this stream's UI — the
 *  stream's own success/abort paths must then be no-ops. */
export function wasKillFinalized(controller: AbortController): boolean {
  return killFinalized.has(controller);
}

/** Abort one session's in-flight stream (a re-send does this before
 *  starting its own; no UI finalization — the stream's own catch handles it). */
export function abortSessionStream(sessionId: string): void {
  const controller = abortControllers.get(sessionId);
  if (controller) {
    controller.abort();
    abortControllers.delete(sessionId);
  }
}

/**
 * Instant kill switch (Stop button, ESC). Strictly synchronous and
 * idempotent: the first re-render after it returns already shows the
 * session idle — no waiting for the AbortError to travel back through
 * the SSE read loop.
 *
 * Order matters: finalize UI state FIRST, mark the controller as
 * kill-finalized BEFORE aborting (so the stream's catch/success paths
 * no-op), then stop every registered subagent. Targets only the given
 * session's stream; background sessions keep streaming. Subagents are
 * global by design — a kill stops them all.
 */
export function killSessionStream(sessionId: string | null): void {
  if (sessionId) {
    // Also stop background shell tasks this session spawned (fire-and-forget:
    // the kill switch stays synchronous; a failed request only means the
    // task finishes on its own like before).
    void fetch('/api/workspace/shell/tasks/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});

    const chat = getChatStore(sessionId).getState();
    const controller = abortControllers.get(sessionId);
    if (chat.isStreaming) {
      // Mirror of the AbortError catch in useChatStream: preserve any
      // partial content as a "(Stopped)" message, nothing when empty.
      // Team activity folded so far is preserved too — the kill switch
      // must not erase a live team card mid-run.
      const { currentStreamContent, currentThinkingContent, thinkingElapsedMs } = chat;
      const killTeamReports = chat.takeTeamReports();
      const abortMsg: ChatMessage | undefined = (currentStreamContent || killTeamReports)
        ? {
            role: 'assistant',
            content: currentStreamContent ? currentStreamContent + '\n\n*(Stopped)*' : '*(Stopped)*',
            timestamp: new Date().toISOString(),
            teamReports: killTeamReports,
            _thinkingMs: thinkingElapsedMs > 0 ? Math.round(thinkingElapsedMs) : undefined,
            _thinkingText: currentThinkingContent || undefined,
          }
        : undefined;
      chat.finishStream(abortMsg);
    }
    if (controller) killFinalized.add(controller);
    abortSessionStream(sessionId);
  }

  const subagents = useSubagentStore.getState();
  for (const [teamId, stop] of Object.entries(subagents.stops)) {
    try {
      stop();
    } catch {
      // One bad callback must not block stopping the rest.
    }
    subagents.unregister(teamId);
  }
}
