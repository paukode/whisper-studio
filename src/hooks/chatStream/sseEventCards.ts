import type { SSEEventData } from '@/types/chat';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useGoalStore } from '@/stores/goalStore';

type ChatState = ReturnType<ReturnType<typeof getChatStore>['getState']>;

/**
 * Declarative "event card" SSE frames. Each drops one tool-use card into the
 * transcript from parsed.<field> (goal_eval also forwards its verdict to the
 * goal store) with no effect on the turn's streaming accumulator. Split out of
 * readSSEStream so the core state machine there stays focused on text, tool
 * traces, approvals, and user questions.
 */
export function renderEventCards(
  parsed: SSEEventData,
  store: () => ChatState,
  sessionId: string,
): void {
  // ── plan_blocked ──
  if (parsed.plan_blocked) {
    // Store for rendering as a special card
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'plan_blocked',
          toolName: 'plan_blocked',
          input: parsed.plan_blocked as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── security_blocked ──
  if (parsed.security_blocked) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'security_blocked',
          toolName: 'security_blocked',
          input: parsed.security_blocked as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── hook_blocked (a blocking hook denied a tool call) ──
  if (parsed.hook_blocked) {
    const hb = parsed.hook_blocked;
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'hook_blocked',
          toolName: 'hook_blocked',
          input: { tool_name: hb.tool_name, reason: hb.reason } as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── stop_hook_feedback (a Stop hook kept the turn going) ──
  if (parsed.stop_hook_feedback) {
    const sf = parsed.stop_hook_feedback;
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'stop_hook_feedback',
          toolName: 'stop_hook_feedback',
          input: { reason: sf.reason, attempt: sf.attempt } as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── goal_eval (completion gate's verdict) ──
  if (parsed.goal_eval) {
    const ge = parsed.goal_eval;
    useGoalStore.getState().applyEval(sessionId, ge);
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'goal_eval',
          toolName: 'goal_eval',
          input: ge as Record<string, unknown>,
          status: ge.verdict === 'achieved' ? 'complete' : 'error',
        },
      ],
    });
  }

  // ── stop_hook_block (a Stop hook refused to end the turn) ──
  if (parsed.stop_hook_block) {
    const sb = parsed.stop_hook_block;
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'stop_hook_block',
          toolName: 'stop_hook_block',
          input: sb as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── goal_cap_reached (gave up after the consecutive-block cap) ──
  if (parsed.goal_cap_reached) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'goal_cap_reached',
          toolName: 'goal_cap_reached',
          input: parsed.goal_cap_reached as Record<string, unknown>,
          status: 'error',
        },
      ],
    });
  }

  // ── workflow_preview (a new workflow awaits approval) ──
  if (parsed.workflow_preview) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'workflow_preview',
          toolName: 'workflow_preview',
          input: parsed.workflow_preview as Record<string, unknown>,
          status: 'complete',
        },
      ],
    });
  }

  // ── workflow_started (a workflow launched — inline run card) ──
  if (parsed.workflow_started) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'workflow_started',
          toolName: 'workflow_started',
          input: parsed.workflow_started as Record<string, unknown>,
          status: 'complete',
        },
      ],
    });
  }

  // ── ci_started (a CI watch launched — inline status card) ──
  if (parsed.ci_started) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'ci_started',
          toolName: 'ci_started',
          input: parsed.ci_started as Record<string, unknown>,
          status: 'complete',
        },
      ],
    });
  }

  // ── ci_diagnosis (autofix findings — shown above the fix preview) ──
  if (parsed.ci_diagnosis) {
    store().addMessage({
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      toolUse: [
        {
          toolId: 'ci_diagnosis',
          toolName: 'ci_diagnosis',
          input: parsed.ci_diagnosis as Record<string, unknown>,
          status: 'complete',
        },
      ],
    });
  }
}
