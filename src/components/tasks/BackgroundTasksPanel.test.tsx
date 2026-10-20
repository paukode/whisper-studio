/**
 * The list under the header pill: rows read their state, a running row has
 * Stop, a finished row has its countdown and dismiss, and the chevron unfolds
 * the details.
 */
import { fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackgroundTasksPanel, formatDuration, homeRelative, statusText, taskLabel } from './BackgroundTasksPanel';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { useDockStore } from '@/stores/dockStore';
import type { BackgroundTaskInfo } from '@/types/backgroundTasks';

const T0 = Date.parse('2026-10-18T10:00:00Z');

function row(over: Partial<BackgroundTaskInfo> = {}): BackgroundTaskInfo {
  return {
    task_id: 't1',
    kind: 'shell',
    session_id: 's1',
    title: 'npm run build',
    command: 'npm run build',
    status: 'running',
    pid: 4242,
    meta: { cwd: '/Users/pat/project' },
    created_at: new Date(T0).toISOString(),
    updated_at: new Date(T0).toISOString(),
    ...over,
  };
}

describe('statusText', () => {
  it('reads elapsed time while running and the outcome afterwards', () => {
    expect(statusText(row(), T0 + 1400)).toBe('running · 1.4s');
    expect(statusText(row({ status: 'completed', exit_code: 0, finished_at: new Date(T0 + 1400).toISOString() }), T0 + 9000)).toBe('exit 0 · 1.4s');
    expect(statusText(row({ kind: 'agent', status: 'completed', exit_code: null, finished_at: new Date(T0 + 64000).toISOString() }), T0)).toBe('done · 1m 04s');
    expect(statusText(row({ status: 'failed', exit_code: 1, finished_at: new Date(T0 + 500).toISOString() }), T0)).toBe('failed · exit 1 · 500ms');
    expect(statusText(row({ status: 'stopped', finished_at: new Date(T0 + 500).toISOString() }), T0)).toBe('stopped · 500ms');
  });

  it('formats durations and home paths', () => {
    expect(formatDuration(820)).toBe('820ms');
    expect(formatDuration(8200)).toBe('8.2s');
    expect(formatDuration(64000)).toBe('1m 04s');
    expect(homeRelative('/Users/pat/project')).toBe('~/project');
    expect(homeRelative('/opt/work')).toBe('/opt/work');
  });
});

describe('BackgroundTasksPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: [], text: '' }) }));
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: true, expanded: {} });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('renders nothing while closed', () => {
    useBackgroundTaskStore.setState({ panelOpen: false });
    const { container } = render(<BackgroundTasksPanel />);
    expect(container.firstChild).toBeNull();
  });

  it('gives a running row Stop and a finished row its countdown and dismiss', () => {
    const finished = row({ task_id: 't2', status: 'completed', exit_code: 0, finished_at: new Date().toISOString() });
    useBackgroundTaskStore.getState().upsert(row());
    useBackgroundTaskStore.getState().upsert(finished);
    const { getByText, getAllByTestId, getByLabelText } = render(<BackgroundTasksPanel />);
    const rows = getAllByTestId('bg-task-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('running');
    expect(getByText('Stop')).toBeTruthy();
    expect(rows[1].textContent).toMatch(/exit 0 · .* · clears in \d+s/);
    fireEvent.click(getByLabelText('Dismiss'));
    expect(useBackgroundTaskStore.getState().tasks.t2).toBeUndefined();
  });

  it('unfolds the details on the chevron and offers Open on an agent row', () => {
    useBackgroundTaskStore.getState().upsert(row({ task_id: 'a1', kind: 'agent', title: 'Probe the org', command: null, meta: { model: 'opus' } }));
    const { getByLabelText, getByText, queryByText } = render(<BackgroundTasksPanel />);
    expect(queryByText(/started/)).toBeNull();
    fireEvent.click(getByLabelText('Show details'));
    expect(getByText(/started .* · opus/)).toBeTruthy();
    expect(getByText('Open')).toBeTruthy();
    expect(useBackgroundTaskStore.getState().expanded.a1).toBe(true);
  });

  it('names a named agent row by its name, keeps the task in the details, and opens under the name', () => {
    const brief = 'USER REQUEST: Review dashboard-code-share, capacity-planner, and emea-delivery-app';
    const agent = (task_id: string, name: string) => row({ task_id, kind: 'agent', title: brief, command: null, meta: { agent_name: name } });
    useBackgroundTaskStore.getState().upsert(agent('a1', 'planner-audit'));
    useBackgroundTaskStore.getState().upsert(agent('a2', 'emea-audit'));
    useDockStore.setState({ panels: [], sizes: [], open: false });
    const { getAllByTestId, getAllByText, getAllByLabelText } = render(<BackgroundTasksPanel />);
    const titles = getAllByTestId('bg-task-row').map((r) => r.querySelector('.bg-tasks-row-title'));
    expect(titles.map((t) => t?.textContent).sort()).toEqual(['emea-audit', 'planner-audit']);
    // The tooltip gives the whole name over the start of the task, which the
    // unfolded details also keep.
    expect(titles.map((t) => t?.getAttribute('title')).sort()).toEqual([`emea-audit\n${brief}`, `planner-audit\n${brief}`]);
    fireEvent.click(getAllByLabelText('Show details')[0]);
    expect(getAllByText(brief)).toHaveLength(1);
    fireEvent.click(getAllByText('Open')[0]);
    const opened = useDockStore.getState().panels.map((p) => p.title);
    expect(opened).toHaveLength(1);
    expect(['emea-audit', 'planner-audit']).toContain(opened[0]);
  });

  it('keeps the title for an agent without a name and for other kinds', () => {
    expect(taskLabel(row({ kind: 'agent', title: 'Probe the org', meta: {} }))).toBe('Probe the org');
    expect(taskLabel(row({ kind: 'agent', title: 'Probe the org', meta: { agent_name: 'probe' } }))).toBe('probe');
    expect(taskLabel(row({ kind: 'shell', title: 'npm run build', meta: { agent_name: 'x' } }))).toBe('npm run build');
  });
});
