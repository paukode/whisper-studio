/**
 * Workflow run state (WS-D), keyed by run_id, plus per-run live agent activity
 * folded from the SSE stream. Two event families ride the `workflow:{run_id}`
 * channel:
 *   - run-level summary events: {type:'phase'|'agent'|'workflow_event'|'snapshot'}
 *   - per-agent detail events: no `type`, carry {agent_id, phase, text/tool_*}
 * The detail events are folded into a per-agent map (TeamAgentReport shape) so
 * WorkflowRunCard renders the same agent rows the chat uses; the full feed of
 * an agent is its record, opened in the dock's agent pane.
 *
 * Zustand v5 rule (see memory): selectors return PRIMITIVES or stable
 * references only — components subscribe to `useWorkflowStore(s => s.runs[id])`
 * or `s => s.activity[id]`, never a freshly-built object.
 */
import { create } from 'zustand';
import type { WorkflowAgentCall, WorkflowRun } from '@/api/workflows';
import type { TeamAgentReport } from '@/types/chat';
import { agentStatusOf, settledStatus } from '@/utils/agentOutcome';

export interface LiveActivity {
  phase: string;
  /** Run-level count of dispatched agents (from type:'agent' events). */
  agents: number;
  cost_usd: number;
  lastLabel: string;
  status: string;
  /** Per-agent live detail, keyed by agent_id (fallback agent_name). */
  agentReports: Record<string, TeamAgentReport>;
  agentOrder: string[];
  /** Agents running right now, and the most that ran at the same time. */
  running: number;
  peak: number;
  /** Agents dispatched per script phase ('' for none), in first-seen order. */
  phaseCounts: Record<string, number>;
  phaseOrder: string[];
}

interface WorkflowState {
  runs: Record<string, WorkflowRun>;
  activity: Record<string, LiveActivity>;
  upsertRun: (run: WorkflowRun) => void;
  applyEvent: (runId: string, ev: Record<string, unknown>) => void;
}

const EMPTY_ACT: LiveActivity = {
  phase: '',
  agents: 0,
  cost_usd: 0,
  lastLabel: '',
  status: 'running',
  agentReports: {},
  agentOrder: [],
  running: 0,
  peak: 0,
  phaseCounts: {},
  phaseOrder: [],
};

/** The most agent calls that ran at the same time, from their journaled
 *  spans. A call that ends as another starts does not overlap it, and a
 *  cache hit (no duration) never counts. */
export function peakConcurrency(calls: readonly WorkflowAgentCall[]): number {
  const marks: Array<[number, number]> = [];
  for (const c of calls) {
    if (typeof c.started_at !== 'number' || typeof c.finished_at !== 'number') continue;
    if (c.finished_at <= c.started_at) continue;
    marks.push([c.started_at, 1], [c.finished_at, -1]);
  }
  // Ends sort before starts at the same instant.
  marks.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let now = 0;
  let peak = 0;
  for (const [, d] of marks) {
    now += d;
    peak = Math.max(peak, now);
  }
  return peak;
}

function journalStatus(status: string | undefined): TeamAgentReport['status'] {
  // 'running': a live run's snapshot lists the calls that have not ended.
  return agentStatusOf(status) === 'running' ? 'running' : settledStatus(status);
}

function countPhase(act: Pick<LiveActivity, 'phaseCounts' | 'phaseOrder'>, phase: string, by = 1) {
  return {
    phaseCounts: { ...act.phaseCounts, [phase]: (act.phaseCounts[phase] ?? 0) + by },
    phaseOrder: act.phaseOrder.includes(phase) ? act.phaseOrder : [...act.phaseOrder, phase],
  };
}

/** Fold a snapshot's journaled agent calls into the live activity, so every
 *  agent of the run stays listed after it finishes and after a reload. A
 *  live row of the same agent keeps its name and task; the journal settles
 *  its status. Counts take the larger of what was seen live and what the
 *  journal holds (a live run's journal lacks the calls still running). */
export function foldAgentCalls(act: LiveActivity, run: WorkflowRun): LiveActivity {
  const calls = run.agent_calls ?? (run.journal ?? []).filter((e) => e.type === 'agent_call') as unknown as WorkflowAgentCall[];
  if (calls.length === 0) return act;
  const agentReports = { ...act.agentReports };
  let agentOrder = act.agentOrder;
  const journalPhases: Record<string, number> = {};
  const journalOrder: string[] = [];
  for (const c of calls) {
    const key = c.agent_id || `seq:${c.seq}`;
    const existing = agentReports[key];
    agentReports[key] = {
      ...(existing ?? {
        name: c.label || `agent ${c.seq + 1}`,
        task: '',
        agent_type: 'general',
        role: 'team' as const,
        agent_id: c.agent_id || undefined,
      }),
      status: journalStatus(c.status),
    };
    if (!agentOrder.includes(key)) agentOrder = [...agentOrder, key];
    const phase = c.phase ?? '';
    journalPhases[phase] = (journalPhases[phase] ?? 0) + 1;
    if (!journalOrder.includes(phase)) journalOrder.push(phase);
  }
  const phaseOrder = [...act.phaseOrder, ...journalOrder.filter((p) => !act.phaseOrder.includes(p))];
  const phaseCounts: Record<string, number> = {};
  for (const p of phaseOrder) phaseCounts[p] = Math.max(act.phaseCounts[p] ?? 0, journalPhases[p] ?? 0);
  return {
    ...act,
    agentReports,
    agentOrder,
    peak: Math.max(act.peak, peakConcurrency(calls)),
    phaseCounts,
    phaseOrder,
  };
}

const DETAIL_PHASES = new Set([
  'started',
  'turn_start',
  'text',
  'tool_call',
  'tool_result',
  'completed',
  'turn_limit',
  'failed',
  'stopped',
]);

function phaseToStatus(phase: string): TeamAgentReport['status'] {
  return agentStatusOf(phase) ?? 'pending';
}

/** Fold one per-agent detail event into the activity's agent map. Keyed by
 *  agent_id (workflow agents carry no stable agent_name), falling back to
 *  agent_name. Returns new agentReports/agentOrder (never mutates prev). */
function foldAgentEvent(
  act: LiveActivity,
  ev: Record<string, unknown>,
): Pick<LiveActivity, 'agentReports' | 'agentOrder'> {
  const key = (ev.agent_id as string) || (ev.agent_name as string) || '';
  if (!key) return { agentReports: act.agentReports, agentOrder: act.agentOrder };

  const agentReports = { ...act.agentReports };
  const agentOrder = act.agentOrder.includes(key) ? act.agentOrder : [...act.agentOrder, key];
  const existing = agentReports[key];

  const displayName =
    (ev.agent_name as string) ||
    existing?.name ||
    (typeof ev.task === 'string' && ev.task ? ev.task.slice(0, 60) : key);

  const base: TeamAgentReport =
    existing ??
    ({
      name: displayName,
      task: (ev.task as string) ?? '',
      agent_type: (ev.agent_type as string) ?? 'general',
      role: ev.agent_type === 'coordinator' ? 'orchestrator' : 'team',
      status: 'pending',
      agent_id: ev.agent_id as string | undefined,
    } satisfies TeamAgentReport);

  agentReports[key] = {
    ...base,
    agent_id: (ev.agent_id as string) ?? base.agent_id,
    agent_type: (ev.agent_type as string) ?? base.agent_type,
    model: (ev.model as string) ?? base.model,
    parent_agent_id: (ev.parent_agent_id as string) ?? base.parent_agent_id,
    name: base.name || displayName,
    // The `started` event carries the full task; latch it for the title.
    task: base.task || ((ev.task as string) ?? ''),
    status: phaseToStatus(ev.phase as string),
    turns_used: (ev.turns_used as number) ?? base.turns_used,
    stop_reason: (ev.stop_reason as string) ?? base.stop_reason,
  };
  return { agentReports, agentOrder };
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  runs: {},
  activity: {},
  upsertRun: (run) =>
    set((s) => ({
      runs: { ...s.runs, [run.run_id]: run },
      activity: { ...s.activity, [run.run_id]: foldAgentCalls(s.activity[run.run_id] ?? EMPTY_ACT, run) },
    })),
  applyEvent: (runId, ev) =>
    set((s) => {
      const type = ev.type as string | undefined;

      if (type === 'snapshot') {
        const run = ev.run as WorkflowRun | undefined;
        if (run) {
          return {
            runs: { ...s.runs, [run.run_id]: run },
            activity: { ...s.activity, [run.run_id]: foldAgentCalls(s.activity[run.run_id] ?? EMPTY_ACT, run) },
          };
        }
        return { runs: s.runs, activity: s.activity };
      }

      const prev = s.activity[runId] ?? EMPTY_ACT;
      const next: LiveActivity = { ...prev };

      // Terminal RUN completion (manager._finalize wraps it as workflow_event).
      // A per-agent `completed` event also has phase==='completed' but no type,
      // so gating on type here stops one finished agent from ending the run.
      if (type === 'workflow_event' && ev.phase === 'completed') {
        next.status = (ev.status as string) || 'done';
        next.agents = (ev.agents_spawned as number) ?? next.agents;
        next.cost_usd = (ev.cost_usd as number) ?? next.cost_usd;
        const existing = s.runs[runId];
        const mergedRun = existing
          ? {
              ...existing,
              status: next.status as WorkflowRun['status'],
              agents_spawned: next.agents,
              cost_usd: next.cost_usd,
              cap_reached: (ev.cap_reached as boolean) ?? existing.cap_reached,
              error: (ev.error as string) || existing.error,
            }
          : existing;
        return {
          runs: mergedRun ? { ...s.runs, [runId]: mergedRun } : s.runs,
          activity: { ...s.activity, [runId]: next },
        };
      }

      // Run-level summary shapes.
      if (type === 'phase' && typeof ev.name === 'string') next.phase = ev.name;
      if (type === 'agent') {
        if (ev.status === 'running' || ev.status === 'cache_hit') {
          next.agents = next.agents + 1;
          Object.assign(next, countPhase(next, typeof ev.phase === 'string' ? ev.phase : ''));
        }
        if (ev.status === 'running') {
          next.running = next.running + 1;
          next.peak = Math.max(next.peak, next.running);
        } else if (ev.status !== 'cache_hit') {
          next.running = Math.max(0, next.running - 1);
        }
        if (typeof ev.label === 'string' && ev.label) next.lastLabel = ev.label;
        if (typeof ev.cost_usd === 'number') next.cost_usd = ev.cost_usd;
      }

      // Per-agent detail events (no type; carry a detail phase).
      if (!type && typeof ev.phase === 'string' && DETAIL_PHASES.has(ev.phase)) {
        const folded = foldAgentEvent(next, ev);
        next.agentReports = folded.agentReports;
        next.agentOrder = folded.agentOrder;
      }

      return { runs: s.runs, activity: { ...s.activity, [runId]: next } };
    }),
}));
