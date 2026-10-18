/**
 * The approval continuation leg of /api/chat.
 *
 * Owns sendApprovalContinuation: POST an approval accept/deny together with
 * the outcome of the action, then feed the continuation response straight
 * back through readSSEStream (./sseStream), so the resumed half of the turn
 * renders through the same parser as the half that paused on the card.
 * The click-to-resume leg of an approval card (running the approved action
 * first) lives in ./approvalLeg and calls in here.
 */
import type { ApprovalOutcome } from '@/api/approval';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import type { ToolUseEvent } from '@/types/chat';
import { toError } from '@/utils/toError';
import {
  buildStoppedMessage,
  registerStreamController,
  releaseStreamController,
  wasKillFinalized,
} from './streamControl';
import { emptyResponseFallback } from './emptyResponse';
import { buildHistoryPayload } from './history';
import { readSSEStream } from './sseStream';

/** What an approval acts on, for the text the model (and the transcript)
 *  reads: the path, else the command, else the card's summary. */
export function approvalTarget(approval: PendingApproval): string {
  return (approval.payload?.path as string | undefined)
    ?? (approval.payload?.command as string | undefined)
    ?? approval.summary
    ?? 'N/A';
}

/** Whether the paused turn picked the approval's result up. `resumed` is
 *  false when the continuation never reached the model (refused at model
 *  resolution, a failed setup or model load, an HTTP or network failure):
 *  an action that ran must then be recorded by the caller, since no turn
 *  will ever say so. A Stop of the continuation counts as resumed; its own
 *  stop path records what the leg did. */
export interface ContinuationResult {
  resumed: boolean;
  reason?: string;
}

/**
 * Send an approval continuation (accept or deny) through the full SSE pipeline.
 *
 * `outcome` is what actually happened when the action ran (see ApprovalOutcome
 * in @/api/approval, ONE definition, shared with the endpoint that produces
 * it). Passing it is what keeps the tool_result truthful: the string used to be
 * a hard-coded "succeeded" that lied to the model whenever the write failed, or
 * was never attempted at all.
 */
export async function sendApprovalContinuation(
  approval: PendingApproval,
  sessionId: string,
  accepted: boolean,
  signal?: AbortSignal,
  outcome?: ApprovalOutcome,
): Promise<ContinuationResult> {
  // Parallel sessions: bind the OWNING session's store from the sessionId
  // this stream was started with. Never resolve the active session here:
  // every store() call below must keep landing in the same session no
  // matter what the user is viewing.
  const store = () => getChatStore(sessionId).getState();
  store().setStreaming(true);

  const target = approvalTarget(approval);
  // Nouns stay action-neutral: this text is what the MODEL reasons from, and
  // the approval registry covers far more than writes: git_push, terminal_run,
  // GitHub API writes, workspace connects. Calling every one of them a
  // "filesystem operation" (or a "change") misdescribed the tool it is
  // answering. The action name is already in the string; the verb carries the
  // outcome.
  let content: string;
  if (!accepted) {
    content = `[User denied] ${approval.action}: ${target}. The user rejected this action; it did not run.`;
  } else if (outcome) {
    if (outcome.ok) {
      const detail = outcome.output ? `\n\n${outcome.output}` : '';
      content = `[User approved] ${approval.action}: ${target}. The action succeeded.${detail}`;
    } else {
      // A command that ran and failed carries its output next to the exit code.
      const detail = [outcome.error, outcome.output].filter(Boolean).join('\n\n') || 'unknown error';
      content = `[User approved but the operation FAILED] ${approval.action}: ${target}. Error: ${detail}`;
    }
  } else {
    // Every accept call site executes the action first and passes its
    // outcome (executeApproval's result, or a synthesized {ok:false} on
    // throw). Reaching here is a caller bug: fail safe rather than tell
    // the model a write happened that nobody performed.
    console.error('sendApprovalContinuation: accepted without an execution outcome');
    content = `[User approved but the operation was NOT executed] ${approval.action}: ${target}. Treat this action as not performed.`;
  }

  // ws_edit_file sets this flag when the old_string matched only after quote
  // normalization. Tell the model its match was inexact so it doesn't assume
  // byte-for-byte file state on the next edit. The flag rides only on edit
  // payloads, so this appends nothing for writes/commands/etc.
  if (accepted && approval.payload?.matched_via_normalization === true) {
    content +=
      ' Note: old_string did not match exactly; it matched only after quote'
      + ' normalization (the file uses typographic quotes that differ from the'
      + ' text sent), and the original quote characters were preserved.';
  }

  const body: Record<string, unknown> = {
    question: '',
    session_id: sessionId,
    // Carry the model AND the per-turn effort/response-length the paused turn
    // ran on, so the continuation resumes on the SAME backend at the SAME
    // settings. Without the model the endpoint falls back to the default
    // cloud model, which would strand a paused local (Gemma) tool turn;
    // without effort and verbosity the post-approval half of the turn
    // silently dropped to the config defaults. Read off the approval, not the
    // picker: the picker is window-wide, so a model chosen in another session
    // while this card waited must not take over this turn.
    ...approval.turnSettings,
    // The conversation so far. Normally unused (the backend restores the
    // stashed paused messages and ignores this), but it is the ONLY context the
    // server has left when that paused state is gone (a backend restart while
    // the card sat on screen). Sending nothing is what made that path answer
    // from an empty conversation.
    history: buildHistoryPayload(store().messages, true),
    approved_tool_result: {
      tool_use_id: approval.toolUseId,
      content,
      accepted,
    },
    session_approvals: store().sessionApprovals,
    session_denials: store().sessionDenials,
  };

  // MCP servers are resolved by the backend from each server's persisted
  // `enabled` flag, so the continuation needs no per-request override.

  // ALWAYS register a controller for this continuation leg so Stop/ESC can
  // reach it: the stream that paused on the card has long since released its
  // own. When the caller passes the signal of the leg that ran the approved
  // action (./approvalLeg), chain it so the session stays claimed without a
  // gap and a cancel there still aborts this leg.
  const ownController = new AbortController();
  registerStreamController(sessionId, ownController);
  if (signal) {
    if (signal.aborted) ownController.abort();
    else signal.addEventListener('abort', () => ownController.abort(), { once: true });
  }
  const continuationSignal = ownController.signal;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: continuationSignal,
    });

    if (!response.ok || !response.body) {
      // Surface the server's own words (e.g. the 409 SESSION_BUSY guidance:
      // "reset it from the chat ⋯ menu") instead of a bare "Continuation
      // failed" the user can't act on.
      const detail = await response.json()
        .then((d: { error?: string }) => d?.error)
        .catch(() => null);
      throw new Error(detail || `Continuation failed (HTTP ${response.status})`);
    }

    const result = await readSSEStream(response, sessionId, continuationSignal, approval.turnSettings);
    // The kill switch may have finalized this leg while the read loop was
    // draining (an aborted signal exits the loop normally). It already
    // committed the traces shown so far; appending them again here would
    // duplicate the activity.
    if (wasKillFinalized(ownController)) return { resumed: true };

    const contTeamReports = store().takeTeamReports();
    const contToolUse: ToolUseEvent[] = result.skillTraces.map(t => ({
      toolId: t.name,
      toolName: t.name,
      input: t.input ?? {},
      result: t.output || undefined,
      status: 'complete' as const,
      previewImage: t.previewImage,
    }));

    // Same empty-answer fallback the fresh-turn path applies (useChatStream's
    // send). Without it a continuation that ended with no text committed
    // NOTHING: the transcript kept only the "Awaiting approval" trace, so an
    // approved turn looked like it had died and the user had to nudge the model
    // by hand. Skipped when the continuation paused again: the new card (or
    // question) IS the visible outcome.
    let contText = result.fullResponse;
    if (
      !contText &&
      !result.hasPendingApprovals &&
      !result.hasUserQuestion &&
      result.flushedSegments === 0
    ) {
      contText = emptyResponseFallback(store());
    }

    // Tool traces are committed even with no text (mirroring the fresh path's
    // approval-pause commit) so the work the continuation did survives the
    // streaming state being cleared.
    if (contText || result.pendingArtifact || result.pendingVisuals.length > 0 || result.pendingPlan || contTeamReports || contToolUse.length > 0) {
      store().addMessage({
        role: 'assistant',
        content: contText,
        timestamp: new Date().toISOString(),
        skills: result.skillsUsed.length > 0 ? result.skillsUsed : undefined,
        traces: result.skillTraces.length > 0 ? result.skillTraces : undefined,
        toolUse: contToolUse.length > 0 ? contToolUse : undefined,
        teamReports: contTeamReports,
        // The continuation can also yield a create_artifact artifact; pin it
        // to this message so the artifact card and the model's text live
        // together in one card and tool chips render only once.
        programArtifact: result.pendingArtifact ?? undefined,
        visuals: result.pendingVisuals.length > 0 ? result.pendingVisuals : undefined,
        plan: result.pendingPlan ?? undefined,
        _thinkingMs: result.thinkingMs > 0 ? Math.round(result.thinkingMs) : undefined,
        _thinkingText: result.thinkingText || undefined,
        _usage: (result.inputTokens > 0 || result.outputTokens > 0)
          ? { input_tokens: result.inputTokens, output_tokens: result.outputTokens }
          : undefined,
        grounding: result.grounding,
        // Taken here, not left for setStreaming(false): that waits while
        // another approval is pending.
        deliveries: store().takeDeliveries(),
      });
    }

    // Refresh workspace tree after approval
    window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
    window.dispatchEvent(new CustomEvent('whisper-git-refresh'));
    return result.refusal === null ? { resumed: true } : { resumed: false, reason: result.refusal };
  } catch (err) {
    if (toError(err).name === 'AbortError') {
      // User stop: the kill switch already finalized the UI (traces included)
      // and a fabricated "failed to continue" bubble would misreport a
      // deliberate stop. A plain re-send abort of this leg gets the same
      // "(Stopped)" commit the fresh-turn path makes, so the prose and tool
      // activity of the leg are not lost; the finally below clears the live
      // trace, mirroring how the success path above commits.
      if (!wasKillFinalized(ownController)) {
        const stoppedMsg = buildStoppedMessage(store());
        if (stoppedMsg) store().addMessage(stoppedMsg);
      }
      return { resumed: true };
    }
    console.error('Approval continuation failed:', err);
    store().addMessage({
      role: 'assistant',
      content: `*Error: Failed to continue after approval: ${toError(err).message}*`,
      timestamp: new Date().toISOString(),
    });
    return { resumed: false, reason: toError(err).message };
  } finally {
    if (ownController) releaseStreamController(sessionId, ownController);
    // Only clear streaming if no more approvals pending
    if (!store().currentApproval && store().approvalQueue.length === 0) {
      store().setStreaming(false);
    }
  }
}
