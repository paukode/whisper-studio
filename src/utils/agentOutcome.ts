/**
 * How an agent run is doing or how it ended, as the UI shows it. ONE place
 * for every agent row, card, feed and record readout; the backend's
 * vocabulary is server/agents/outcome.py.
 *
 * The status is coarse (completed, stopped, failed while settled) and the
 * stop reason names why a run ended (turn_limit, deadline, cost_cap,
 * refused_calls, cancelled, error). Records and saved transcripts written
 * before the status followed the reason can carry a `turn_limit` phase or
 * status, and a workflow call says `incomplete` for a limit stop: both read
 * as stopped here.
 */
import type { TeamAgentReport } from '@/types/chat';

export type AgentStatus = TeamAgentReport['status'];

/** The status a phase or a status string means; null for one that says
 *  nothing about it (team_started, team_completed, finished). */
export function agentStatusOf(phaseOrStatus: string | undefined): AgentStatus | null {
  switch (phaseOrStatus) {
    case 'pending':
      return 'pending';
    case 'started':
    case 'turn_start':
    case 'text':
    case 'tool_call':
    case 'tool_result':
    case 'running':
      return 'running';
    case 'completed':
      return 'completed';
    case 'stopped':
    case 'turn_limit':
    case 'incomplete':
      return 'stopped';
    case 'failed':
    case 'error':
      return 'failed';
    default:
      return null;
  }
}

const LIMIT_REASONS = new Set(['turn_limit', 'deadline', 'cost_cap', 'refused_calls']);

/** A run that has ended: its status, with the stop reason winning over a
 *  status an older record wrote (a limit stop once read "completed"). */
export function settledStatus(status: string | undefined, stopReason?: string): AgentStatus {
  if (stopReason && (LIMIT_REASONS.has(stopReason) || stopReason === 'cancelled')) return 'stopped';
  if (stopReason === 'error') return 'failed';
  const s = agentStatusOf(status);
  return s === 'stopped' || s === 'failed' ? s : 'completed';
}

/** The stop reason of a run that ended, named when the record did not. */
export function stopReasonOf(phaseOrStatus: string | undefined, stopReason?: string): string {
  if (stopReason) return stopReason;
  if (phaseOrStatus === 'turn_limit') return 'turn_limit';
  const s = agentStatusOf(phaseOrStatus);
  return s === 'failed' ? 'error' : s === 'stopped' ? 'cancelled' : 'completed';
}

export type OutcomeTone = 'done' | 'err' | 'neutral';

export interface Outcome {
  /** A word or two, for a chip. */
  chip: string;
  /** A short phrase, for a row or a feed line. */
  line: string;
  tone: OutcomeTone;
}

const OUTCOMES: Record<string, Outcome> = {
  pending: { chip: 'starting', line: 'starting', tone: 'neutral' },
  running: { chip: 'running', line: 'running', tone: 'neutral' },
  completed: { chip: 'finished', line: 'done', tone: 'done' },
  turn_limit: { chip: 'turn limit', line: 'stopped at the turn limit', tone: 'neutral' },
  deadline: { chip: 'time limit', line: 'stopped at the time limit', tone: 'neutral' },
  cost_cap: { chip: 'cost cap', line: 'stopped at the cost cap', tone: 'neutral' },
  refused_calls: { chip: 'refused calls', line: 'stopped after repeated refused calls', tone: 'neutral' },
  cancelled: { chip: 'cancelled', line: 'stopped', tone: 'neutral' },
  stopped: { chip: 'stopped', line: 'stopped', tone: 'neutral' },
  error: { chip: 'failed', line: 'failed', tone: 'err' },
  failed: { chip: 'failed', line: 'failed', tone: 'err' },
};

/** What to show for a run: by its stop reason once it has ended, else by its
 *  status. A finished run takes the done tone and a failure the error tone;
 *  a limit or a cancel stays neutral (its report was written). */
export function outcomeOf(phaseOrStatus: string | undefined, stopReason?: string): Outcome {
  const s = agentStatusOf(phaseOrStatus);
  const ended = s === null || s === 'completed' || s === 'stopped' || s === 'failed';
  const key = (ended ? stopReason : undefined) ?? (phaseOrStatus === 'turn_limit' ? 'turn_limit' : s ?? phaseOrStatus ?? '');
  return OUTCOMES[key] ?? { chip: key, line: key, tone: 'neutral' };
}
