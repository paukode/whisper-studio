import { beforeEach, describe, expect, it } from 'vitest';
import { useWorkflowStore } from './workflowStore';

const RUN = 'run1';

function apply(ev: Record<string, unknown>) {
  useWorkflowStore.getState().applyEvent(RUN, ev);
}

describe('workflowStore.applyEvent', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ runs: {}, activity: {}, order: [] });
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
    expect(agent.events).toHaveLength(3);
    expect(agent.events.map((e) => e.phase)).toEqual(['started', 'tool_call', 'text']);
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
