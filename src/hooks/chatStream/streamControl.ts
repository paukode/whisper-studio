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
import type { ChatState } from '@/stores/chatStore';
import type { ChatMessage, ToolUseEvent } from '@/types/chat';

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

/** Placeholder result for a step the user cut short before its tool
 *  returned, so the committed row still has something to expand. */
export const STOPPED_TOOL_RESULT = '[Stopped by user]';

/** Freeze a live tool trace for the transcript: a step still running (or
 *  never started) becomes 'stopped' so no committed row spins forever. */
function stopToolEntry(t: ToolUseEvent): ToolUseEvent {
  if (t.status !== 'running' && t.status !== 'pending') return t;
  return { ...t, status: 'stopped', result: t.result || STOPPED_TOOL_RESULT };
}

/** Close every live team report in place: each agent still running (or
 *  never started) gets a synthetic `stopped` event and the team a synthetic
 *  `team_completed`, folded through the same reducer the SSE events use, so
 *  the card stops spinning and reads exactly like a server-side stop. Must
 *  run BEFORE takeTeamReports, which detaches the map from the store. */
function closeLiveTeams(chat: ChatState): void {
  for (const report of Object.values(chat.liveTeamReports)) {
    if (report.status !== 'running') continue;
    for (const [key, agent] of Object.entries(report.agents)) {
      if (agent.status !== 'running' && agent.status !== 'pending') continue;
      // Keyed the way _agentKey files agents: the map key IS the name the
      // team_started scaffold (or the agent_id fallback) used.
      chat.foldTeamEvent({
        team_id: report.team_id,
        agent_name: key,
        agent_id: agent.agent_id,
        phase: 'stopped',
      });
    }
    chat.foldTeamEvent({ team_id: report.team_id, phase: 'team_completed' });
  }
}

/**
 * The assistant message a stopped turn leaves behind, built from the live
 * accumulators of the given chat state: partial prose (with the "(Stopped)"
 * marker), the tool activity shown so far (running steps frozen as
 * 'stopped'), the live team reports (closed) and the thinking shown so far.
 * Returns undefined when the turn had produced none of that yet, so an abort
 * during the pure thinking phase appends nothing.
 *
 * Side effects on `chat`: closes live teams and takes (clears) the live team
 * reports. The caller commits the result with finishStream (fresh turn) or
 * addMessage (continuation leg) so the live trace is cleared in the same
 * pass the message lands.
 */
export function buildStoppedMessage(chat: ChatState): ChatMessage | undefined {
  const { currentStreamContent, currentThinkingContent, thinkingElapsedMs } = chat;
  const toolUse = chat.currentStreamToolUse.map(stopToolEntry);
  closeLiveTeams(chat);
  const teamReports = chat.takeTeamReports();
  if (!currentStreamContent && toolUse.length === 0 && !teamReports) return undefined;
  return {
    role: 'assistant',
    content: currentStreamContent ? currentStreamContent + '\n\n*(Stopped)*' : '*(Stopped)*',
    timestamp: new Date().toISOString(),
    stopped: true,
    toolUse: toolUse.length > 0 ? toolUse : undefined,
    teamReports,
    _thinkingMs: thinkingElapsedMs > 0 ? Math.round(thinkingElapsedMs) : undefined,
    _thinkingText: currentThinkingContent || undefined,
  };
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
      // Same commit the AbortError catch in useChatStream performs: partial
      // prose, the tool activity shown so far and any live team card all
      // survive as one "(Stopped)" message; nothing is appended when the
      // turn had produced none of them yet.
      chat.finishStream(buildStoppedMessage(chat));
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
