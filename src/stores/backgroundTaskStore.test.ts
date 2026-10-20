import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LINGER_MS, useBackgroundTaskStore } from './backgroundTaskStore';
import type { BackgroundTaskInfo, TaskEventPayload } from '@/types/backgroundTasks';

function ev(over: Partial<TaskEventPayload> = {}): TaskEventPayload {
  return {
    event_type: 'task_started',
    task_id: 't1',
    kind: 'shell',
    title: 'sleep 60',
    status: 'running',
    timestamp: new Date().toISOString(),
    ...over,
  };
}

function row(over: Partial<BackgroundTaskInfo> = {}): BackgroundTaskInfo {
  return {
    task_id: 'a',
    kind: 'agent',
    session_id: 's9',
    title: 'explore',
    status: 'running',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...over,
  };
}

describe('backgroundTaskStore', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: false, expanded: {} });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('applyEvent inserts a running task and maintains runningCount', () => {
    useBackgroundTaskStore.getState().applyEvent('s1', ev());
    const state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1.status).toBe('running');
    expect(state.tasks.t1.session_id).toBe('s1');
    expect(state.runningCount).toBe(1);
  });

  it("keeps a named agent's name from its live frame where a hydrated row has it", () => {
    const store = useBackgroundTaskStore.getState();
    store.applyEvent('s1', ev({ task_id: 'a1', kind: 'agent', title: 'USER REQUEST: same brief', agent_name: 'planner-audit' }));
    expect(useBackgroundTaskStore.getState().tasks.a1.meta).toEqual({ agent_name: 'planner-audit' });
    // A later frame without a name keeps the one the row has.
    store.applyEvent('s1', ev({ task_id: 'a1', kind: 'agent', event_type: 'task_completed', status: 'completed', agent_name: '' }));
    expect(useBackgroundTaskStore.getState().tasks.a1.meta?.agent_name).toBe('planner-audit');
  });

  it('a finished row keeps its exit code for LINGER_MS, then leaves on its own', () => {
    const store = useBackgroundTaskStore.getState();
    store.applyEvent('s1', ev());
    store.applyEvent('s1', ev({ event_type: 'task_completed', status: 'completed', exit_code: 0, result_tail: 'done' }));
    let state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1.status).toBe('completed');
    expect(state.tasks.t1.exit_code).toBe(0);
    expect(state.tasks.t1.result_text).toBe('done');
    expect(state.runningCount).toBe(0);
    vi.advanceTimersByTime(LINGER_MS - 1);
    expect(useBackgroundTaskStore.getState().tasks.t1).toBeDefined();
    vi.advanceTimersByTime(1);
    state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1).toBeUndefined();
  });

  it('dismiss removes a row at once, with its unfolded state', () => {
    const store = useBackgroundTaskStore.getState();
    store.applyEvent('s1', ev());
    store.toggleExpanded('t1');
    expect(useBackgroundTaskStore.getState().expanded.t1).toBe(true);
    store.dismiss('t1');
    const state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1).toBeUndefined();
    expect(state.expanded.t1).toBeUndefined();
    expect(state.runningCount).toBe(0);
  });

  it('a row running again before its linger ends stays (a resumed agent keeps its id)', () => {
    const store = useBackgroundTaskStore.getState();
    store.applyEvent('s1', ev({ task_id: 'a', kind: 'agent', title: 'probe' }));
    store.applyEvent('s1', ev({ task_id: 'a', kind: 'agent', event_type: 'task_completed', status: 'completed' }));
    vi.advanceTimersByTime(LINGER_MS / 2);
    store.applyEvent('s1', ev({ task_id: 'a', kind: 'agent', event_type: 'task_started', status: 'running' }));
    vi.advanceTimersByTime(LINGER_MS);
    const state = useBackgroundTaskStore.getState();
    expect(state.tasks.a.status).toBe('running');
    expect(state.tasks.a.finished_at).toBeNull();
    expect(state.runningCount).toBe(1);
  });

  it('completion preserves fields hydrate provided that events lack', () => {
    const hydrated = row({
      task_id: 't1',
      kind: 'shell',
      session_id: 's1',
      title: 'sleep 60',
      command: 'sleep 60',
      pid: 4242,
      output_path: '/tmp/t1.txt',
      created_at: '2026-07-18T04:59:00Z',
    });
    const store = useBackgroundTaskStore.getState();
    store.upsert(hydrated);
    store.applyEvent('s1', ev({ event_type: 'task_stopped', status: 'stopped' }));
    const t = useBackgroundTaskStore.getState().tasks.t1;
    expect(t.command).toBe('sleep 60');
    expect(t.pid).toBe(4242);
    expect(t.output_path).toBe('/tmp/t1.txt');
    expect(t.created_at).toBe('2026-07-18T04:59:00Z');
    expect(t.status).toBe('stopped');
  });

  it('hydrate keeps running rows and rows finished within the linger window', async () => {
    const now = Date.now();
    const rows: BackgroundTaskInfo[] = [
      row({ task_id: 'a' }),
      row({ task_id: 'b', status: 'completed', finished_at: new Date(now - LINGER_MS / 2).toISOString() }),
      row({ task_id: 'c', status: 'completed', finished_at: new Date(now - LINGER_MS * 2).toISOString() }),
    ];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: rows }) }));
    await useBackgroundTaskStore.getState().hydrate();
    const state = useBackgroundTaskStore.getState();
    expect(Object.keys(state.tasks).sort()).toEqual(['a', 'b']);
    expect(state.runningCount).toBe(1);
    vi.advanceTimersByTime(LINGER_MS / 2 + 1);
    expect(Object.keys(useBackgroundTaskStore.getState().tasks)).toEqual(['a']);
  });

  it('stopTask marks the row stopped when the server accepts', async () => {
    useBackgroundTaskStore.getState().applyEvent('s1', ev());
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ stopped: true }) }));
    await expect(useBackgroundTaskStore.getState().stopTask('t1')).resolves.toBe(true);
    expect(useBackgroundTaskStore.getState().tasks.t1.status).toBe('stopped');
  });
});
