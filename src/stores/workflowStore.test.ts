import { beforeEach, describe, expect, it } from 'vitest';
import { peakConcurrency, useWorkflowStore } from './workflowStore';
import type { WorkflowRun } from '@/api/workflows';

const RUN = 'run1';

function apply(ev: Record<string, unknown>) {
  useWorkflowStore.getState().applyEvent(RUN, ev);
}

describe('workflowStore.applyEvent', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ runs: {}, activity: {} });
  });

  it('folds per-agent detail events into a per-agent report keyed by agent_id', () => {
    apply({ agent_id: 'a1', agent_name: 'review:bugs', phase: 'started', task: 'find bugs' });
    apply({ agent_id: 'a1', phase: 'tool_call', tool_name: 'ws_grep', tool_input_preview: 'foo' });
    apply({ agent_id: 'a1', phase: 'text', text: 'looking...' });

    const act = useWorkflowStore.getState().activity[RUN];
    expect(act.agentOrder).toEqual(['a1']);
    const agent = act.agentReports['a1'];
    expect(agent.name).toBe('review:bugs');
    expect(agent.task).toBe('find bugs');
    expect(agent.status).toBe('running');
    expect(agent.agent_id).toBe('a1');
  });

  it('tracks multiple agents independently', () => {
    apply({ agent_id: 'a1', phase: 'started', task: 'one' });
    apply({ agent_id: 'a2', phase: 'started', task: 'two' });
    apply({ agent_id: 'a1', phase: 'completed', turns_used: 4 });

    const act = useWorkflowStore.getState().activity[RUN];
    expect(act.agentOrder).toEqual(['a1', 'a2']);
    expect(act.agentReports['a1'].status).toBe('completed');
    expect(act.agentReports['a1'].turns_used).toBe(4);
    expect(act.agentReports['a2'].status).toBe('running');
  });

  it('does NOT mark the run terminal on a per-agent completed event', () => {
    useWorkflowStore.getState().upsertRun({
      run_id: RUN,
      name: 'wf',
      status: 'running',
      agents_spawned: 0,
      tokens_in: 0,
      tokens_out: 0,
      cost_usd: 0,
      cap_reached: false,
      error: '',
    });
    apply({ agent_id: 'a1', phase: 'completed', turns_used: 2, status: 'completed' });

    expect(useWorkflowStore.getState().runs[RUN].status).toBe('running');
    expect(useWorkflowStore.getState().activity[RUN].status).not.toBe('completed');
  });

  it('marks the run terminal only on the run-level workflow_event', () => {
    useWorkflowStore.getState().upsertRun({
      run_id: RUN,
      name: 'wf',
      status: 'running',
      agents_spawned: 0,
      tokens_in: 0,
      tokens_out: 0,
      cost_usd: 0,
      cap_reached: false,
      error: '',
    });
    apply({ type: 'workflow_event', phase: 'completed', status: 'done', agents_spawned: 3, cost_usd: 0.42 });

    const s = useWorkflowStore.getState();
    expect(s.runs[RUN].status).toBe('done');
    expect(s.runs[RUN].agents_spawned).toBe(3);
    expect(s.activity[RUN].status).toBe('done');
  });

  it('accumulates run-level summary from type:agent and type:phase events', () => {
    apply({ type: 'phase', name: 'Review' });
    apply({ type: 'agent', status: 'running', label: 'review:bugs' });
    apply({ type: 'agent', status: 'running', label: 'review:perf', cost_usd: 0.1 });

    const act = useWorkflowStore.getState().activity[RUN];
    expect(act.phase).toBe('Review');
    expect(act.agents).toBe(2);
    expect(act.lastLabel).toBe('review:perf');
    expect(act.cost_usd).toBe(0.1);
  });
});

describe('workflowStore: finished agents stay listed', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ runs: {}, activity: {} });
  });

  const run = (over: Partial<WorkflowRun> = {}): WorkflowRun => ({
    run_id: RUN, name: 'wf', status: 'done', agents_spawned: 3, tokens_in: 0, tokens_out: 0, cost_usd: 0, cap_reached: false, error: '',
    agent_calls: [
      { seq: 0, phase: 'plan', label: 'planner', status: 'completed', agent_id: 'a0', started_at: 0, finished_at: 10 },
      { seq: 1, phase: 'review', label: 'r1', status: 'completed', agent_id: 'a1', started_at: 10, finished_at: 30 },
      { seq: 2, phase: 'review', label: 'r2', status: 'failed', agent_id: 'a2', started_at: 12, finished_at: 20 },
      { seq: 3, phase: 'review', label: 'cached', status: 'cache_hit', started_at: 15, finished_at: 15 },
    ],
    ...over,
  });

  it('a snapshot after a reload lists every journaled agent with its status', () => {
    apply({ type: 'snapshot', run: run() });
    const act = useWorkflowStore.getState().activity[RUN];
    expect(act.agentOrder).toHaveLength(4);
    expect(act.agentOrder.map((k) => act.agentReports[k].name)).toEqual(['planner', 'r1', 'r2', 'cached']);
    expect(act.agentReports.a2.status).toBe('failed');
    expect(act.agentReports.a0.agent_id).toBe('a0');
    expect(act.phaseOrder).toEqual(['plan', 'review']);
    expect(act.phaseCounts).toEqual({ plan: 1, review: 3 });
    expect(act.peak).toBe(2);
  });

  it('a reload mid-run lists the agents still running, and their live events update the same rows', () => {
    apply({
      type: 'snapshot',
      run: run({
        status: 'running',
        agent_calls: [
          { seq: 0, phase: 'draft', label: 'draft:a', status: 'completed', agent_id: 'a0', started_at: 0, finished_at: 5 },
          { seq: 1, phase: 'draft', label: 'draft:b', status: 'running', agent_id: 'a1', started_at: 0 },
        ],
      }),
    });
    let act = useWorkflowStore.getState().activity[RUN];
    expect(act.agentOrder).toEqual(['a0', 'a1']);
    expect(act.agentReports.a1.status).toBe('running');

    apply({ agent_id: 'a1', agent_name: 'draft:b', phase: 'completed' });
    act = useWorkflowStore.getState().activity[RUN];
    expect(act.agentOrder).toEqual(['a0', 'a1']);
    expect(act.agentReports.a1.status).toBe('completed');
  });

  it('a live row keeps its name and task, and the journal settles its status', () => {
    apply({ agent_id: 'a1', agent_name: 'review:bugs', phase: 'started', task: 'find bugs' });
    apply({ type: 'snapshot', run: run() });
    const a1 = useWorkflowStore.getState().activity[RUN].agentReports.a1;
    expect(a1.name).toBe('review:bugs');
    expect(a1.task).toBe('find bugs');
    expect(a1.status).toBe('completed');
  });

  it('counts the peak and the phases from live agent events', () => {
    apply({ type: 'agent', status: 'running', phase: 'review', seq: 0 });
    apply({ type: 'agent', status: 'running', phase: 'review', seq: 1 });
    apply({ type: 'agent', status: 'completed', phase: 'review', seq: 0 });
    apply({ type: 'agent', status: 'running', phase: 'fix', seq: 2 });
    apply({ type: 'agent', status: 'cache_hit', phase: 'fix', seq: 3 });
    const act = useWorkflowStore.getState().activity[RUN];
    expect(act.peak).toBe(2);
    expect(act.running).toBe(2);
    expect(act.phaseCounts).toEqual({ review: 2, fix: 2 });
    expect(act.agents).toBe(4);
  });

  it('peakConcurrency: back to back calls do not overlap, open spans are skipped', () => {
    expect(peakConcurrency([
      { seq: 0, started_at: 0, finished_at: 5 },
      { seq: 1, started_at: 5, finished_at: 9 },
      { seq: 2 },
    ])).toBe(1);
    expect(peakConcurrency(Array.from({ length: 16 }, (_, i) => ({ seq: i, started_at: 0, finished_at: 1 + i })))).toBe(16);
  });

  it('a refresh folds the journal too', () => {
    useWorkflowStore.getState().upsertRun(run());
    expect(useWorkflowStore.getState().activity[RUN].agentOrder).toHaveLength(4);
  });
});
