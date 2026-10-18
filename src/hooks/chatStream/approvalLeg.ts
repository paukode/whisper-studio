/**
 * The user's answer to an approval card, from the click to the resumed turn.
 *
 * The turn that asked has already ended its stream by the time the card is on
 * screen (the server stashes the paused turn and closes with [DONE]). An
 * approved action can then run for a long time (a shell command up to minutes)
 * before its continuation opens a stream again. That whole window used to look
 * idle: no Stop button and no ESC, the composer started a SECOND turn from a
 * typed "continue" that the continuation then overwrote, a Stop was ignored
 * (the turn resumed when the action returned), and the runtime registry could
 * evict the session so the continuation's reply landed in a store nobody saved.
 *
 * The leg therefore claims its session for the whole window, before the card
 * goes away: it marks the session streaming (with a status line saying what
 * is happening) and registers a controller, so the Stop button, ESC, the
 * composer's steer routing, the parallel ceiling and eviction all see a busy
 * session through the same state they already read for a streaming turn.
 */
import type { StoreApi } from 'zustand/vanilla';
import { executeApproval, type ApprovalOutcome } from '@/api/approval';
import { getChatStore, holdForApprovedAction } from '@/stores/sessionRuntimes';
import type { ChatState, PendingApproval } from '@/stores/chatStore';
import { useSessionStore } from '@/stores/sessionStore';
import type { ChatMessage } from '@/types/chat';
import { useUIStore } from '@/stores/uiStore';
import { toError } from '@/utils/toError';
import { approvalTarget, sendApprovalContinuation } from './approvalContinuation';
import { patchCommittedRows } from './committedRows';
import { toolMetaWithStats } from './toolMeta';
import {
  hasLiveStream,
  registerStreamController,
  releaseStreamController,
  wasKillFinalized,
} from './streamControl';

/** What the streaming bubble says while the approved action runs. */
export const APPROVED_ACTION_STATUS = 'Running the approved action';

/**
 * Answer an approval card: `accepted` runs the action and resumes the turn
 * with its outcome, otherwise the turn resumes with the denial.
 * `rememberForSession` is the card's "Yes, all" / "Block" choice.
 *
 * Returns false, and leaves the card where it is, when the session already
 * has a live stream: resuming now would write a second turn into the store
 * that stream is streaming into.
 */
export async function answerApproval(
  approval: PendingApproval,
  accepted: boolean,
  rememberForSession: boolean,
): Promise<boolean> {
  const sessionId = approval.sessionId;
  // Bound once: a session deleted mid-leg must not be recreated by a late write.
  const chat = getChatStore(sessionId);
  if (hasLiveStream(sessionId)) {
    useUIStore.getState().addToast({
      type: 'error',
      message: 'Another turn is running in this session. Stop it first, then answer the approval.',
      duration: 5000,
      key: 'approval-while-streaming',
    });
    return false;
  }

  if (rememberForSession) {
    chat.getState().setSessionApproval(approval.category, accepted ? 'allow' : 'deny');
  }

  if (!accepted) {
    // Nothing runs before the continuation, which claims the session itself
    // synchronously, so there is no window to cover. The paused call's row
    // says it was refused: the continuation carries no result for it.
    chat.getState().clearCurrentApproval();
    patchDeniedRow(chat, sessionId, approval);
    await sendApprovalContinuation(approval, sessionId, false);
    return true;
  }

  const leg = new AbortController();
  // Stopped, or replaced by a new send, while the action runs: the turn does
  // not resume, but the action already started, so the transcript (which
  // every later turn's history is built from) must say what it did. The row is
  // written the moment the leg is aborted, from inside abort(), before
  // whoever aborted adds anything: it sits where the stop happened, not after
  // a newer question, and the turn that replaced this one already reads it.
  const stop: { row: ChatMessage | null } = { row: null };
  const onAbort = () => {
    stop.row = stoppedActionMessage(approval, wasKillFinalized(leg) ? 'stopped' : 'replaced', null);
    chat.getState().addMessage(stop.row);
  };
  leg.signal.addEventListener('abort', onAbort, { once: true });
  // The outcome row is still to be written after a Stop lets go of the
  // stream, so the session stays busy (never evicted) until the action returns.
  const releaseAction = holdForApprovedAction(sessionId);
  chat.getState().setStreaming(true);
  chat.getState().setStreamStatus(APPROVED_ACTION_STATUS);
  registerStreamController(sessionId, leg);
  chat.getState().clearCurrentApproval();
  try {
    const outcome = await runApprovedAction(approval);
    // From here an abort belongs to the continuation, which records its own stop.
    leg.signal.removeEventListener('abort', onAbort);
    // The paused call's row says what the action did, whatever comes next.
    patchApprovedRow(chat, sessionId, approval, outcome);
    if (stop.row) {
      recordOutcome(chat, sessionId, stop.row, approval, outcome);
      return true;
    }
    // The continuation registers its own controller and chains this one, so
    // the session stays claimed without a gap until its stream takes over.
    const cont = await sendApprovalContinuation(approval, sessionId, true, leg.signal, outcome);
    if (!cont.resumed) {
      // Refused (a switch to Local mode while the card waited, a removed
      // model) or failed: the action ran, and no turn will ever say so, so
      // the transcript later turns are built from records it here.
      chat.getState().addMessage(stoppedActionMessage(approval, 'refused', outcome, cont.reason));
      useSessionStore.getState().debouncedSave(sessionId);
    }
    return true;
  } finally {
    releaseAction();
    releaseStreamController(sessionId, leg);
  }
}

/** Fill the action's outcome into the row written when its leg was stopped,
 *  in place, and save: the row count does not change, which is what the
 *  runtime's own save trigger keys on. A row the user deleted meanwhile
 *  stays deleted. */
function recordOutcome(
  chat: StoreApi<ChatState>,
  sessionId: string,
  row: ChatMessage,
  approval: PendingApproval,
  outcome: ApprovalOutcome,
): void {
  const messages = chat.getState().messages;
  const idx = messages.findIndex((m) => m.timestamp === row.timestamp && m.content === row.content);
  if (idx < 0) return;
  const next = messages.slice();
  next[idx] = {
    ...stoppedActionMessage(approval, row.stopped ? 'stopped' : 'replaced', outcome),
    timestamp: row.timestamp,
  };
  chat.getState().setMessages(next);
  useSessionStore.getState().debouncedSave(sessionId);
}

/** The row keeps a preview of the result, cut where the server cuts the
 *  skill_result preview a live turn persists (server/tool_executor.py). */
const RESULT_PREVIEW_CHARS = 2000;

/** Write the approved action's outcome onto the row the paused turn
 *  committed for its call (committedRows.patchCommittedRows): the result
 *  replaces "Awaiting approval: ...", the status says how it went, a
 *  command's exit status becomes the row's exit code, and a write's diff
 *  stats become the row's meta (the inline path gets the same numbers from
 *  its skill_result frame). The text is the one the inline path gives the
 *  model (server/tool_approvals.py inline_result_text). */
export function patchApprovedRow(
  chat: StoreApi<ChatState>,
  sessionId: string,
  approval: PendingApproval,
  outcome: ApprovalOutcome,
): void {
  if (!approval.toolUseId) return;
  let result: string;
  if (outcome.ok) {
    result = outcome.output || `[OK] ${approval.action}`;
  } else {
    // A command that ran and failed carries its output next to the exit code.
    const error = `Error: ${outcome.error || 'unknown error'}`;
    result = outcome.output ? `${error}\n\n${outcome.output}` : error;
  }
  if (result.length > RESULT_PREVIEW_CHARS) result = `${result.slice(0, RESULT_PREVIEW_CHARS)}...`;
  const stats = outcome.stats;
  patchCommittedRows(chat, sessionId, approval.toolUseId, {
    result,
    status: outcome.ok ? 'complete' : outcome.stopped ? 'stopped' : 'error',
    isError: !outcome.ok,
    exitCode: outcome.exit_code ?? null,
    ...(stats ? { meta: (old) => toolMetaWithStats(old, stats) } : {}),
  });
}

/** The row of a call the user refused on its card: an error that says so,
 *  the way an inline session-rule denial reads ("Write a.py failed"), not
 *  the "Awaiting approval" it was committed with. */
function patchDeniedRow(chat: StoreApi<ChatState>, sessionId: string, approval: PendingApproval): void {
  if (!approval.toolUseId) return;
  patchCommittedRows(chat, sessionId, approval.toolUseId, {
    result: `[Denied by user] ${approval.action}: ${approvalTarget(approval)}`,
    status: 'error',
    isError: true,
    exitCode: null,
  });
}

/** Run the approved action. Never throws: a failed request becomes a truthful
 *  FAILED outcome, so the model learns the action did not happen. */
async function runApprovedAction(approval: PendingApproval): Promise<ApprovalOutcome> {
  let outcome: ApprovalOutcome;
  try {
    // Single executor: the backend looks up the spec and runs its registered
    // function. No per-action switch on the frontend.
    // The call's id lets a write keep its edit for the diff dialog.
    outcome = await executeApproval({
      action: approval.action,
      payload: approval.payload,
      tool_use_id: approval.toolUseId,
    });
  } catch (err) {
    console.error('Approval execution failed:', err);
    outcome = { ok: false, error: toError(err).message };
  }
  if (!outcome.ok) {
    useUIStore.getState().addToast({
      type: 'error',
      message: `Approval failed: ${outcome.error ?? 'unknown error'}`,
      duration: 6000,
      key: 'approval-apply-error',
    });
  } else {
    // An action may have connected a new workspace (e.g. git_clone with
    // open=true). Switch the active workspace so the panel opens; the backend
    // already updated its config, this brings the UI in line.
    if (outcome.ws_folder_opened) {
      useUIStore.getState().setWsConnected(true, outcome.ws_folder_opened);
    }
    window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
  }
  return outcome;
}

/** The transcript row for an approved action whose turn did not continue:
 *  the user pressed Stop, a new send replaced the turn, or the continuation
 *  was refused or failed (`reason`). `outcome` is null while the action is
 *  still running. */
function stoppedActionMessage(
  approval: PendingApproval,
  end: 'stopped' | 'replaced' | 'refused',
  outcome: ApprovalOutcome | null,
  reason?: string,
): ChatMessage {
  const what = `The approved action ${approval.action} (${approvalTarget(approval)})`;
  const when = end === 'stopped'
    ? 'was already running when the turn was stopped'
    : end === 'replaced'
      ? 'was still running when a new message replaced the turn'
      : `ran, but the turn could not continue (${reason ?? 'unknown error'})`;
  // Which actions a Stop reaches is the server's call (a shell command is
  // killed, a git action finishes), so the interim row claims neither.
  let result: string;
  if (outcome === null) {
    result = 'Its result will be recorded here.';
  } else if (outcome.stopped) {
    result = 'Stop ended it before it finished.';
  } else if (outcome.ok) {
    result = 'It completed.';
  } else {
    // A command that ran and failed carries its output next to the exit code.
    const detail = [outcome.error, outcome.output].filter(Boolean).join('\n\n') || 'unknown error';
    result = `It failed: ${detail}.`;
  }
  return {
    role: 'assistant',
    content: `${end === 'stopped' ? '*(Stopped)* ' : ''}${what} ${when}. ${result} The turn did not continue.`,
    timestamp: new Date().toISOString(),
    ...(end === 'stopped' ? { stopped: true as const } : {}),
  };
}
