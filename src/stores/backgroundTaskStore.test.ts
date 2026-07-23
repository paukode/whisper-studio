import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useBackgroundTaskStore } from './backgroundTaskStore';
import type { TaskEventPayload } from '@/types/chat';
import type { BackgroundTaskInfo } from '@/types/backgroundTasks';

function ev(over: Partial<TaskEventPayload> = {}): TaskEventPayload {
  return {
    event_type: 'task_started',
    task_id: 't1',
    kind: 'shell',
    title: 'sleep 60',
    status: 'running',
    timestamp: '2026-07-18T05:00:00Z',
    ...over,
  };
}

describe('backgroundTaskStore', () => {
  beforeEach(() => {
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: false });
  });

  it('applyEvent inserts a running task and maintains runningCount', () => {
    useBackgroundTaskStore.getState().applyEvent('s1', ev());
    const state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1.status).toBe('running');
    expect(state.tasks.t1.session_id).toBe('s1');
    expect(state.runningCount).toBe(1);
  });

  it('completion event flips status and decrements runningCount', () => {
    const store = useBackgroundTaskStore.getState();
    store.applyEvent('s1', ev());
    store.applyEvent(
      's1',
      ev({
        event_type: 'task_completed',
        status: 'completed',
        exit_code: 0,
        result_tail: 'done',
        timestamp: '2026-07-18T05:00:05Z',
      }),
    );
    const state = useBackgroundTaskStore.getState();
    expect(state.tasks.t1.status).toBe('completed');
    expect(state.tasks.t1.result_text).toBe('done');
    expect(state.runningCount).toBe(0);
  });

  it('completion preserves fields hydrate provided that events lack', () => {
    const hydrated: BackgroundTaskInfo = {
      task_id: 't1',
      kind: 'shell',
      session_id: 's1',
      title: 'sleep 60',
      command: 'sleep 60',
      status: 'running',
      output_path: '/tmp/t1.txt',
      created_at: '2026-07-18T04:59:00Z',
      updated_at: '2026-07-18T04:59:00Z',
    };
    const store = useBackgroundTaskStore.getState();
    store.upsert(hydrated);
    store.applyEvent('s1', ev({ event_type: 'task_stopped', status: 'stopped' }));
    const t = useBackgroundTaskStore.getState().tasks.t1;
    expect(t.command).toBe('sleep 60');
    expect(t.output_path).toBe('/tmp/t1.txt');
    expect(t.created_at).toBe('2026-07-18T04:59:00Z');
    expect(t.status).toBe('stopped');
  });

  it('hydrate replaces the map from the API', async () => {
    const rows: BackgroundTaskInfo[] = [
      {
        task_id: 'a',
        kind: 'agent',
        session_id: 's9',
        title: 'explore',
        status: 'running',
        created_at: 'x',
        updated_at: 'x',
      },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: rows }) }),
    );
    await useBackgroundTaskStore.getState().hydrate();
    const state = useBackgroundTaskStore.getState();
    expect(Object.keys(state.tasks)).toEqual(['a']);
    expect(state.runningCount).toBe(1);
    vi.unstubAllGlobals();
  });
});
