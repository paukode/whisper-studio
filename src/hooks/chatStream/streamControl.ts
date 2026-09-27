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
import { subagentsOf, useSubagentStore } from '@/stores/subagentStore';
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

/** When the turn each controller's stream belongs to began (epoch seconds).
 *  Stop sends it as `since`, so the backend stops only the commands and
 *  background tasks this turn started, never a dev server an earlier turn
 *  left running. */
const streamStartedAt = new WeakMap<AbortController, number>();

/** Start of each session's current turn. Only a fresh send starts a turn;
 *  every other registration (the leg that runs an approved action, the
 *  approval continuation, an answered question card) is another leg of that
 *  same turn and keeps its start, so what the turn began in an earlier leg
 *  (a background task, a command handed off at the budget, the approved
 *  command itself) stays within reach of a Stop pressed in a later one. */
const turnStartedAt = new Map<string, number>();

/** Per session, the writer of streamed text the pacer has not painted yet
 *  (./streamPacer). Stop builds its message from the store, so it writes
 *  that text out first or the stopped reply would lose its last words. */
const pendingTextFlushers = new Map<string, () => void>();

/** Register this stream's pending-text writer; returns the unregister, which
 *  leaves a newer stream's writer in place. */
export function registerPendingTextFlusher(sessionId: string, flush: () => void): () => void {
  pendingTextFlushers.set(sessionId, flush);
  return () => {
    if (pendingTextFlushers.get(sessionId) === flush) pendingTextFlushers.delete(sessionId);
  };
}

/** Track a stream's controller for the session. `startsTurn` marks a fresh
 *  send (useChatStream): it alone opens a new turn. Any other caller, present
 *  or future, continues the session's current turn without having to say so;
 *  with no turn on record it starts one now. */
export function registerStreamController(
  sessionId: string,
  controller: AbortController,
  opts: { startsTurn?: boolean } = {},
): void {
  abortControllers.set(sessionId, controller);
  const inherited = opts.startsTurn ? undefined : turnStartedAt.get(sessionId);
  const since = inherited ?? Date.now() / 1000;
  turnStartedAt.set(sessionId, since);
  streamStartedAt.set(controller, since);
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

/** Whether a stream (a turn, a continuation, or an approved action still
 *  running) currently owns this session. */
export function hasLiveStream(sessionId: string): boolean {
  return abortControllers.has(sessionId);
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
 * no-op), then stop the session's own /subagent runs. Targets only the
 * given session: other sessions' streams and /subagent runs keep going.
 */
export function killSessionStream(sessionId: string | null): void {
  if (sessionId) {
    // Paint what the pacer still holds, then read the store it updated.
    pendingTextFlushers.get(sessionId)?.();
    const chat = getChatStore(sessionId).getState();
    const controller = abortControllers.get(sessionId);
    const since = controller ? streamStartedAt.get(controller) : undefined;
    if (since !== undefined) {
      // Also stop the shell work this stream started: its foreground
      // commands and the background tasks it launched. Nothing from an
      // earlier turn, and nothing at all when no stream is running.
      // Fire-and-forget: the kill switch stays synchronous; a failed request
      // only means the work finishes on its own.
      void fetch('/api/workspace/shell/tasks/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, since }),
      }).catch(() => {});
    }
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
  for (const teamId of subagentsOf(subagents, sessionId)) {
    try {
      subagents.stops[teamId]?.();
    } catch {
      // One bad callback must not block stopping the rest.
    }
    subagents.unregister(teamId);
  }
}
