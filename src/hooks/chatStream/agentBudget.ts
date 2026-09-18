/**
 * Budget readout for one agent row, derived from its progress events.
 *
 * The runtime stamps every turn_start event with the cap (max_turns, live
 * extensions included), seconds elapsed and allowed, the estimated spend so
 * far and whether the coming round is the final one. The card turns that
 * into bars and a state pill; nothing here is computed from wall-clock time
 * on the client, so a reloaded history renders the same bars.
 */
import type { TeamAgentReport, TeamProgressEvent } from '@/types/chat';

export type AgentBudgetState = 'working' | 'finishing' | 'done' | 'limit' | 'salvaged' | 'failed';

export interface AgentBudget {
  /** Rounds spent so far and the cap (null cap = unknown). */
  turns: { used: number; cap: number | null };
  /** Seconds elapsed and allowed (null = no time budget). */
  time: { elapsed: number | null; cap: number | null };
  /** Estimated spend so far in USD (null = unknown). */
  costUsd: number | null;
  state: AgentBudgetState;
}

/** Fraction of a budget reserved for the final, reporting round. */
export const FINALE_FRACTION = 0.1;

function lastWith<K extends keyof TeamProgressEvent>(
  events: TeamProgressEvent[],
  key: K,
): TeamProgressEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev[key] !== undefined && ev[key] !== null) return ev;
  }
  return undefined;
}

export function agentBudget(agent: TeamAgentReport): AgentBudget {
  const events = agent.events ?? [];
  const capEv = lastWith(events, 'max_turns');
  const turnEv = lastWith(events, 'turn');
  const cap = capEv?.max_turns ?? null;
  // turn_start announces the NEXT round; rounds spent so far is one less.
  const fromTurn = turnEv?.turn !== undefined ? Math.max(0, turnEv.turn - 1) : 0;
  const used = agent.turns_used ?? fromTurn;
  const elapsedEv = lastWith(events, 'elapsed_s');
  const deadlineEv = lastWith(events, 'deadline_s');
  const costEv = lastWith(events, 'cost_usd');

  let state: AgentBudgetState;
  const reason = agent.stop_reason;
  if (agent.status === 'running' || agent.status === 'pending') {
    const stateEv = lastWith(events, 'budget_state');
    state = stateEv?.budget_state === 'finishing' ? 'finishing' : 'working';
  } else if (reason === 'cancelled' || agent.status === 'stopped') {
    state = 'salvaged';
  } else if (reason === 'turn_limit' || reason === 'deadline' || reason === 'cost_cap' || agent.status === 'turn_limit') {
    state = 'limit';
  } else if (agent.status === 'failed' || reason === 'error') {
    state = 'failed';
  } else {
    state = 'done';
  }

  return {
    turns: { used, cap },
    time: {
      elapsed: elapsedEv?.elapsed_s ?? null,
      cap: deadlineEv?.deadline_s ?? null,
    },
    costUsd: costEv?.cost_usd ?? null,
    state,
  };
}

export const STATE_LABEL: Record<AgentBudgetState, string> = {
  working: 'working',
  finishing: 'finishing up',
  done: 'done',
  limit: 'hit limit',
  salvaged: 'salvaged',
  failed: 'failed',
};

export function formatSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return rest ? `${m}m${String(rest).padStart(2, '0')}s` : `${m}m`;
}
