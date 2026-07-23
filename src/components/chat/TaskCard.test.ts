import { describe, it, expect } from 'vitest';
import { computeTaskCheckpoints } from './TaskCard';
import { taskProgress } from '@/stores/taskStore';
import type { ToolUseEvent } from '@/types/chat';

interface Task { id: string; subject: string; status: string }

const create = (t: Task, i = 0): ToolUseEvent => ({
  toolId: `create-${t.id}-${i}`,
  toolName: 'task_create',
  input: { subject: t.subject },
  result: JSON.stringify({ task: t, task_id: t.id }),
  status: 'complete',
});
const update = (t: Task, i = 0): ToolUseEvent => ({
  toolId: `update-${t.id}-${i}`,
  toolName: 'task_update',
  input: { task_id: t.id, status: t.status },
  result: JSON.stringify({ task: t }),
  status: 'complete',
});
const list = (tasks: Task[], i = 0): ToolUseEvent => ({
  toolId: `list-${i}`,
  toolName: 'task_list',
  input: {},
  result: JSON.stringify({ tasks }),
  status: 'complete',
});
const stop = (id: string, i = 0): ToolUseEvent => ({
  toolId: `stop-${id}-${i}`,
  toolName: 'task_stop',
  input: { task_id: id },
  result: '',
  status: 'complete',
});
const msg = (toolUse: ToolUseEvent[]) => ({ toolUse });

describe('computeTaskCheckpoints', () => {
  it('shows a row when the cumulative state changes and skips unchanged turns', () => {
    const a = { id: 't1', subject: 'A', status: 'completed' };
    const b = { id: 't2', subject: 'B', status: 'pending' };
    const c = { id: 't3', subject: 'C', status: 'pending' };
    const messages = [
      msg([]), // 0: user, no tools
      msg([create(a), create({ ...b }), create({ ...c }), update(a)]), // 1: 1/3 -> checkpoint
      msg([list([a, b, c])]), // 2: still 1/3 -> no checkpoint
      msg([update({ ...b, status: 'completed' }), update({ ...c, status: 'completed' })]), // 3: 3/3 -> checkpoint
      msg([list([{ ...a }, { ...b, status: 'completed' }, { ...c, status: 'completed' }])]), // 4: still 3/3 -> no checkpoint
    ];

    const cps = computeTaskCheckpoints(messages);
    expect([...cps.keys()].sort((x, y) => x - y)).toEqual([1, 3]);

    const first = cps.get(1)!;
    expect(taskProgress(first)).toMatchObject({ done: 1, total: 3 });
    const second = cps.get(3)!;
    expect(taskProgress(second)).toMatchObject({ done: 3, total: 3 });
  });

  it('returns an empty map when no message has task tools', () => {
    const cps = computeTaskCheckpoints([msg([]), msg([{ toolId: 'r', toolName: 'ws_read_file', input: {}, status: 'complete' }])]);
    expect(cps.size).toBe(0);
  });

  it('never emits a checkpoint for an empty cumulative list (all stopped)', () => {
    const a = { id: 't1', subject: 'A', status: 'pending' };
    const messages = [
      msg([create(a)]), // 1: 0/1 -> checkpoint
      msg([stop('t1')]), // 2: empty -> no checkpoint
    ];
    const cps = computeTaskCheckpoints(messages);
    expect([...cps.keys()]).toEqual([0]);
    expect(cps.has(1)).toBe(false);
  });
});
