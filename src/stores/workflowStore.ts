/**
 * Workflow run state (WS-D), keyed by run_id, plus per-run live agent activity
 * folded from the SSE stream. Two event families ride the `workflow:{run_id}`
 * channel:
 *   - run-level summary events: {type:'phase'|'agent'|'workflow_event'|'snapshot'}
 *   - per-agent detail events: no `type`, carry {agent_id, phase, text/tool_*}
 * The detail events are folded into a per-agent map (TeamAgentReport shape) so
 * WorkflowRunCard can render the same expandable live log TeamReportCard uses.
 *
 * Zustand v5 rule (see memory): selectors return PRIMITIVES or stable
 * references only — components subscribe to `useWorkflowStore(s => s.runs[id])`
 * or `s => s.activity[id]`, never a freshly-built object.
 */
import { create } from 'zustand';
import type { WorkflowRun } from '@/api/workflows';
import type { TeamAgentReport, TeamProgressEvent } from '@/types/chat';

/** Upper bound on retained events per agent (mirrors teamProgress). */
const MAX_EVENTS_PER_AGENT = 400;

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
}

interface WorkflowState {
  runs: Record<string, WorkflowRun>;
  activity: Record<string, LiveActivity>;
  order: string[]; // run_ids, newest first
  upsertRun: (run: WorkflowRun) => void;
  setRuns: (runs: WorkflowRun[]) => void;
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
};

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
  if (phase === 'completed') return 'completed';
  if (phase === 'turn_limit') return 'turn_limit';
  if (phase === 'failed') return 'failed';
  if (phase === 'stopped') return 'stopped';
  if (
    phase === 'started' ||
    phase === 'turn_start' ||
    phase === 'text' ||
    phase === 'tool_call' ||
    phase === 'tool_result'
  ) {
    return 'running';
  }
  return 'pending';
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
      events: [],
      agent_id: ev.agent_id as string | undefined,
    } satisfies TeamAgentReport);

  const events = [...base.events, ev as unknown as TeamProgressEvent].slice(-MAX_EVENTS_PER_AGENT);
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
    events,
  };
  return { agentReports, agentOrder };
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  runs: {},
  activity: {},
  order: [],
  upsertRun: (run) =>
    set((s) => ({
      runs: { ...s.runs, [run.run_id]: run },
      order: s.order.includes(run.run_id) ? s.order : [run.run_id, ...s.order],
    })),
  setRuns: (runs) =>
    set(() => ({
      runs: Object.fromEntries(runs.map((r) => [r.run_id, r])),
      order: runs.map((r) => r.run_id),
    })),
  applyEvent: (runId, ev) =>
    set((s) => {
      const type = ev.type as string | undefined;

      if (type === 'snapshot') {
        const run = ev.run as WorkflowRun | undefined;
        if (run) {
          return {
            runs: { ...s.runs, [run.run_id]: run },
            order: s.order.includes(run.run_id) ? s.order : [run.run_id, ...s.order],
            activity: s.activity,
          };
        }
        return { runs: s.runs, order: s.order, activity: s.activity };
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
          order: s.order,
          activity: { ...s.activity, [runId]: next },
        };
      }

      // Run-level summary shapes.
      if (type === 'phase' && typeof ev.name === 'string') next.phase = ev.name;
      if (type === 'agent') {
        if (ev.status === 'running' || ev.status === 'cache_hit') next.agents = next.agents + 1;
        if (typeof ev.label === 'string' && ev.label) next.lastLabel = ev.label;
        if (typeof ev.cost_usd === 'number') next.cost_usd = ev.cost_usd;
      }

      // Per-agent detail events (no type; carry a detail phase).
      if (!type && typeof ev.phase === 'string' && DETAIL_PHASES.has(ev.phase)) {
        const folded = foldAgentEvent(next, ev);
        next.agentReports = folded.agentReports;
        next.agentOrder = folded.agentOrder;
      }

      return { runs: s.runs, order: s.order, activity: { ...s.activity, [runId]: next } };
    }),
}));
