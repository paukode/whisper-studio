import type { SSEEventData, ToolUseEvent } from '@/types/chat';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useGoalStore } from '@/stores/goalStore';

type ChatState = ReturnType<ReturnType<typeof getChatStore>['getState']>;

/**
 * Declarative "event card" SSE frames. Each drops one tool-use card into the
 * transcript from parsed.<field> (goal_eval also forwards its verdict to the
 * goal store) with no effect on the turn's streaming accumulator. Split out of
 * readSSEStream so the core state machine there stays focused on text, tool
 * traces, approvals, and user questions.
 *
 * `flushSegment` closes off the prose streamed so far as its own message before
 * a card is appended — see emitCard.
 */
export function renderEventCards(
  parsed: SSEEventData,
  store: () => ChatState,
  sessionId: string,
  flushSegment: () => void,
): void {
  /**
   * Append one standalone card message, in stream order.
   *
   * The flush is the ordering guarantee. Cards are appended the instant their
   * frame arrives, but the turn's text is only committed when the stream ends
   * — so without a flush every card jumped ahead of the entire discussion, and
   * an approval card ended up above the model's own explanation of it with the
   * Approve button off-screen until the user scrolled back up. Flushing first
   * commits the text so far as its own message, so the card follows the prose
   * that introduced it and the model's next words follow the card.
   */
  const emitCard = (
    toolName: string,
    input: Record<string, unknown>,
    status: ToolUseEvent['status'],
  ): void => {
    flushSegment();
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [{ toolId: toolName, toolName, input, status }],
    });
  };

  // ── plan_blocked ──
  if (parsed.plan_blocked) {
    emitCard('plan_blocked', parsed.plan_blocked, 'error');
  }

  // ── security_blocked ──
  if (parsed.security_blocked) {
    emitCard('security_blocked', parsed.security_blocked, 'error');
  }

  // ── hook_blocked (a blocking hook denied a tool call) ──
  if (parsed.hook_blocked) {
    const hb = parsed.hook_blocked;
    emitCard('hook_blocked', { tool_name: hb.tool_name, reason: hb.reason }, 'error');
  }

  // ── goal_eval (completion gate's verdict) ──
  if (parsed.goal_eval) {
    const ge = parsed.goal_eval;
    useGoalStore.getState().applyEval(sessionId, ge);
    emitCard('goal_eval', ge as Record<string, unknown>, ge.verdict === 'achieved' ? 'complete' : 'error');
  }

  // ── stop_hook_block (a Stop hook refused to end the turn) ──
  if (parsed.stop_hook_block) {
    emitCard('stop_hook_block', parsed.stop_hook_block as Record<string, unknown>, 'error');
  }

  // ── goal_cap_reached (gave up after the consecutive-block cap) ──
  if (parsed.goal_cap_reached) {
    emitCard('goal_cap_reached', parsed.goal_cap_reached as Record<string, unknown>, 'error');
  }

  // ── workflow_preview (a new workflow awaits approval) ──
  if (parsed.workflow_preview) {
    emitCard('workflow_preview', parsed.workflow_preview as Record<string, unknown>, 'complete');
  }

  // ── workflow_started (a workflow launched — inline run card) ──
  if (parsed.workflow_started) {
    emitCard('workflow_started', parsed.workflow_started as Record<string, unknown>, 'complete');
  }

  // ── ci_started (a CI watch launched — inline status card) ──
  if (parsed.ci_started) {
    emitCard('ci_started', parsed.ci_started as Record<string, unknown>, 'complete');
  }

  // ── ci_diagnosis (autofix findings — shown above the fix preview) ──
  if (parsed.ci_diagnosis) {
    emitCard('ci_diagnosis', parsed.ci_diagnosis as Record<string, unknown>, 'complete');
  }
}
