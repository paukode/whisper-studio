/**
 * Render contract for BackgroundTaskCard: the three lifecycle shapes
 * (started pill with stop, completion card with exit/duration/tail,
 * failed/stopped variants) and the store-driven "still running" gate on
 * the start pill's Stop button.
 */
import { render, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { BackgroundTaskCard } from './BackgroundTaskCard';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import type { TaskEventPayload } from '@/types/chat';

function ev(over: Partial<TaskEventPayload> = {}): TaskEventPayload {
  return {
    event_type: 'task_started',
    task_id: 't1',
    kind: 'shell',
    title: 'npm run build',
    status: 'running',
    timestamp: '2026-07-18T05:00:00Z',
    ...over,
  };
}

describe('BackgroundTaskCard', () => {
  beforeEach(() => {
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: false });
  });

  it('renders a start pill with a Stop button while running', () => {
    useBackgroundTaskStore.getState().applyEvent('s1', ev());
    const { getByText, getByTitle } = render(<BackgroundTaskCard event={ev()} />);
    expect(getByText('npm run build')).toBeTruthy();
    expect(getByText(/running/)).toBeTruthy();
    expect(getByTitle('Stop this background task')).toBeTruthy();
  });

  it('start pill drops Stop once the store says the task finished', () => {
    useBackgroundTaskStore
      .getState()
      .applyEvent('s1', ev({ event_type: 'task_completed', status: 'completed' }));
    const { queryByTitle, getByText } = render(<BackgroundTaskCard event={ev()} />);
    expect(queryByTitle('Stop this background task')).toBeNull();
    expect(getByText(/started/)).toBeTruthy();
  });

  it('stop button calls the stop endpoint', () => {
    useBackgroundTaskStore.getState().applyEvent('s1', ev());
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ stopped: true }) });
    vi.stubGlobal('fetch', fetchMock);
    const { getByTitle } = render(<BackgroundTaskCard event={ev()} />);
    fireEvent.click(getByTitle('Stop this background task'));
    expect(fetchMock).toHaveBeenCalledWith('/api/background-tasks/t1/stop', { method: 'POST' });
    vi.unstubAllGlobals();
  });

  it('renders a completion card with exit code, duration, and output tail', () => {
    const { getByText, container } = render(
      <BackgroundTaskCard
        event={ev({
          event_type: 'task_completed',
          status: 'completed',
          exit_code: 0,
          duration_ms: 64_000,
          result_tail: 'build ok',
        })}
      />,
    );
    const status = container.querySelector('.task-event-status');
    expect(status?.textContent).toContain('finished');
    expect(status?.textContent).toContain('exit 0');
    expect(status?.textContent).toContain('1m 04s');
    expect(getByText('npm run build')).toBeTruthy();
    expect(container.querySelector('.task-event-tail')?.textContent).toBe('build ok');
    expect(container.querySelector('.task-event-done-ok')).toBeTruthy();
  });

  it('renders failed and stopped variants with their state classes', () => {
    const failed = render(
      <BackgroundTaskCard
        event={ev({ event_type: 'task_failed', status: 'failed', exit_code: 3 })}
      />,
    );
    expect(failed.container.querySelector('.task-event-done-failed')).toBeTruthy();
    expect(failed.container.querySelector('.task-event-status')?.textContent).toContain('exit 3');

    const stopped = render(
      <BackgroundTaskCard event={ev({ event_type: 'task_stopped', status: 'stopped' })} />,
    );
    expect(stopped.container.querySelector('.task-event-done-stopped')).toBeTruthy();
    expect(stopped.container.querySelector('.task-event-status')?.textContent).toContain('stopped');
  });
});

describe('BackgroundTaskCard historical replay', () => {
  it('start pill without a store row shows no Stop button (session resume)', () => {
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: false });
    const { queryByTitle, getByText } = render(<BackgroundTaskCard event={ev()} />);
    expect(queryByTitle('Stop this background task')).toBeNull();
    expect(getByText(/started/)).toBeTruthy();
  });
});
