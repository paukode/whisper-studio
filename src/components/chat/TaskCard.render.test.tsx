import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import type { SessionTask } from '@/stores/taskStore';

// Mutable snapshot the store mocks read from, so each test can set its own
// task state before rendering. Both TaskCard and TasksPanel read the SAME
// object here — that is the point: one source, so they can never disagree.
const shared = vi.hoisted(() => ({
  currentSessionId: 'sess-1' as string | null,
  tasksBySession: {} as Record<string, { id: string; subject: string; status: string }[]>,
}));

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: unknown) => unknown) => sel({ currentSessionId: shared.currentSessionId }),
}));

vi.mock('@/stores/taskStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/taskStore')>();
  return {
    ...actual, // keep the real taskProgress / normalizeTasks
    useTaskStore: (sel: (s: unknown) => unknown) => sel({ tasksBySession: shared.tasksBySession }),
  };
});

vi.mock('@/stores/dockStore', () => ({
  useDockStore: (sel: (s: unknown) => unknown) => sel({ openPanel: vi.fn() }),
}));

import { TaskCard } from './TaskCard';
import { TasksPanel } from '@/components/tasks/TasksPanel';

const t = (id: string, status: string): SessionTask => ({ id, subject: id.toUpperCase(), status });

beforeEach(() => {
  shared.currentSessionId = 'sess-1';
  shared.tasksBySession = {};
});

describe('TaskCard / TasksPanel — chip and panel agree, no stuck "in progress"', () => {
  it('current chip reads the store and agrees with the panel at 4/4 "all done"', () => {
    shared.tasksBySession = {
      'sess-1': [t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed')],
    };

    // Live chip: no `tasks` prop, so it reads the store (the current row).
    const chip = render(<TaskCard />);
    expect(chip.container.textContent).toContain('4/4');
    expect(chip.container.textContent).toContain('all done');
    expect(chip.container.textContent).not.toContain('in progress');

    // The Tasks panel reads the same store.
    const panel = render(<TasksPanel />);
    expect(panel.container.textContent).toContain('4 of 4');
  });

  it('a historical checkpoint keeps its 1/4 count but never shows "in progress"', () => {
    // A frozen point-in-time snapshot where task 2 was running.
    const snapshot = [t('t1', 'completed'), t('t2', 'in_progress'), t('t3', 'pending'), t('t4', 'pending')];
    const { container } = render(<TaskCard tasks={snapshot} historical />);
    expect(container.textContent).toContain('1/4');
    expect(container.textContent).not.toContain('in progress');
  });

  it('the live row (not historical) DOES surface a running task', () => {
    const snapshot = [t('t1', 'completed'), t('t2', 'in_progress'), t('t3', 'pending'), t('t4', 'pending')];
    const { container } = render(<TaskCard tasks={snapshot} historical={false} />);
    expect(container.textContent).toContain('1/4');
    expect(container.textContent).toContain('in progress');
  });

  it('once the session is complete, even the live store-backed row is "all done"', () => {
    shared.tasksBySession = {
      'sess-1': [t('t1', 'completed'), t('t2', 'completed'), t('t3', 'completed'), t('t4', 'completed')],
    };
    // historical omitted -> live; store all complete -> resolves to done.
    const { container } = render(<TaskCard />);
    expect(container.textContent).toContain('all done');
    expect(container.textContent).not.toContain('in progress');
  });
});
