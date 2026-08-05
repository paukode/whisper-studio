import { describe, it, expect } from 'vitest';
import { computeTaskCheckpoints, resolveTaskCheckpoints } from './TaskCard';
import { taskProgress, type SessionTask } from '@/stores/taskStore';
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

describe('resolveTaskCheckpoints — current row is single-sourced from the store', () => {
  const t = (id: string, status: string): SessionTask => ({ id, subject: id.toUpperCase(), status });
  const s = (...tasks: SessionTask[]) => tasks;

  // Reproduces the reported bug: the committed tool calls only ever recorded
  // task1 completing (the completions of tasks 2-4 never landed in a committed
  // message's toolUse), so the DERIVED checkpoint freezes at 1/4 "in progress"
  // while the authoritative task store advances to 4/4.
  const frozenMessages = [
    msg([]), // 0: user
    msg([
      create({ id: 't1', subject: 'A', status: 'completed' }),
      create({ id: 't2', subject: 'B', status: 'pending' }),
      create({ id: 't3', subject: 'C', status: 'pending' }),
      create({ id: 't4', subject: 'D', status: 'pending' }),
      update({ id: 't1', subject: 'A', status: 'completed' }),
    ]), // 1: derived cumulative freezes at 1/4
    msg([{ toolId: 'r', toolName: 'ws_read_file', input: {}, status: 'complete' }]), // 2: final report, no task tools
  ];

  it('derived checkpoint alone is stale (the bug): shows 1/4 not the real 4/4', () => {
    const cps = computeTaskCheckpoints(frozenMessages);
    expect([...cps.keys()]).toEqual([1]);
    expect(taskProgress(cps.get(1)!)).toMatchObject({ done: 1, total: 4 });
  });

  it('binds the most-recent row to the store so the chip matches the panel (4/4)', () => {
    const store = s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed'));
    const { checkpoints, liveIdx } = resolveTaskCheckpoints(frozenMessages, store, false);
    expect(liveIdx).toBe(1);
    // The current inline chip now reads the SAME array the Tasks panel reads.
    expect(checkpoints.get(1)).toBe(store);
    const chip = taskProgress(checkpoints.get(1)!);
    const panel = taskProgress(store); // TasksPanel reads exactly this
    expect(chip).toEqual(panel);
    expect(chip).toMatchObject({ done: 4, total: 4, active: false });
  });

  it('tracks the store as it advances 1/4 -> 2/4 -> 3/4 -> 4/4', () => {
    const stages: Array<[SessionTask[], number]> = [
      [s(t('t1', 'completed'), t('t2', 'in_progress'), t('t3', 'pending'), t('t4', 'pending')), 1],
      [s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'in_progress'), t('t4', 'pending')), 2],
      [s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'in_progress')), 3],
      [s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed')), 4],
    ];
    for (const [store, expectedDone] of stages) {
      const { checkpoints } = resolveTaskCheckpoints(frozenMessages, store, false);
      expect(taskProgress(checkpoints.get(1)!).done).toBe(expectedDone);
    }
    // Final stage: all done, nothing in progress -> the chip resolves complete.
    const final = resolveTaskCheckpoints(frozenMessages, stages[3][0], false);
    expect(taskProgress(final.checkpoints.get(1)!)).toMatchObject({ done: 4, total: 4, active: false });
  });

  it('preserves earlier historical checkpoints while binding only the last', () => {
    const messages = [
      msg([
        create({ id: 't1', subject: 'A', status: 'completed' }),
        create({ id: 't2', subject: 'B', status: 'pending' }),
        create({ id: 't3', subject: 'C', status: 'pending' }),
        create({ id: 't4', subject: 'D', status: 'pending' }),
        update({ id: 't1', subject: 'A', status: 'completed' }),
      ]), // 0: 1/4 -> historical checkpoint
      msg([update({ id: 't2', subject: 'B', status: 'in_progress' })]), // 1: 1/4 + active -> new checkpoint
    ];
    const store = s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed'));
    const { checkpoints, liveIdx } = resolveTaskCheckpoints(messages, store, false);
    expect(liveIdx).toBe(1);
    // Earlier checkpoint keeps its point-in-time snapshot (1/4)...
    expect(taskProgress(checkpoints.get(0)!)).toMatchObject({ done: 1, total: 4 });
    // ...and only the last one is bound to the authoritative store (4/4).
    expect(checkpoints.get(1)).toBe(store);
  });

  it('stays historical while streaming (live row is the StreamingMessage row)', () => {
    const store = s(t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed'));
    const { checkpoints, liveIdx } = resolveTaskCheckpoints(frozenMessages, store, true);
    expect(liveIdx).toBe(-1);
    // Not bound to the store while streaming -> derived value retained.
    expect(checkpoints.get(1)).not.toBe(store);
    expect(taskProgress(checkpoints.get(1)!)).toMatchObject({ done: 1, total: 4 });
  });

  it('suppresses all rows when the store is authoritatively empty', () => {
    const { checkpoints, liveIdx } = resolveTaskCheckpoints(frozenMessages, [], false);
    expect(checkpoints.size).toBe(0);
    expect(liveIdx).toBe(-1);
  });

  it('uses derived checkpoints as-is when the store is not yet hydrated', () => {
    const { checkpoints, liveIdx } = resolveTaskCheckpoints(frozenMessages, undefined, false);
    expect(liveIdx).toBe(-1);
    expect(taskProgress(checkpoints.get(1)!)).toMatchObject({ done: 1, total: 4 });
  });
});
