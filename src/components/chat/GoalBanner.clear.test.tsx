import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GoalBanner } from './GoalBanner';
import { useGoalStore } from '@/stores/goalStore';
import { useUIStore } from '@/stores/uiStore';

/**
 * Clear removes the banner only after the server dropped the goal: a failed
 * DELETE keeps the banner (the gate still runs) and says why.
 */

const sid = 'goal-banner-clear';

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('GoalBanner clear', () => {
  beforeEach(() => {
    useUIStore.setState({ toasts: [] });
    act(() => useGoalStore.getState().setGoal(sid, 'write the summary', true));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    useGoalStore.getState().clearGoal(sid);
  });

  it('keeps the goal and shows the reason when the DELETE fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
      init?.method === 'DELETE'
        ? respond(500, { error: 'database is locked' })
        : respond(200, { goal: 'write the summary', state: { active: true } }),
    );
    render(<GoalBanner sessionId={sid} />);
    fireEvent.click(screen.getByLabelText('Clear goal'));
    await waitFor(() =>
      expect(useUIStore.getState().toasts.map((t) => t.message)).toContain(
        'Could not clear the goal: database is locked',
      ),
    );
    expect(screen.getByText('write the summary')).toBeInTheDocument();
  });

  it('removes the banner once the server cleared the goal', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
      init?.method === 'DELETE'
        ? respond(200, { ok: true })
        : respond(200, { goal: 'write the summary', state: { active: true } }),
    );
    render(<GoalBanner sessionId={sid} />);
    fireEvent.click(screen.getByLabelText('Clear goal'));
    await waitFor(() => expect(screen.queryByText('write the summary')).toBeNull());
  });
});
