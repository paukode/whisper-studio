import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/ci', () => ({
  ciStatus: vi.fn(),
  getWatch: vi.fn(),
  planAutofix: vi.fn(),
  stopWatch: vi.fn(),
}));

import { ciStatus, getWatch, planAutofix } from '@/api/ci';
import { CIStatusCard } from './CIStatusCard';
import { useCIStore } from '@/stores/ciStore';
import { useSessionStore } from '@/stores/sessionStore';

/**
 * A CI watch or lookup that fails says why on the card: the watch's gh
 * message, the status lookup's error, and the autofix request's error.
 */

beforeEach(() => {
  useCIStore.setState({ watches: {} });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('CIStatusCard errors', () => {
  it('shows the failed watch reason from ci_result', () => {
    useCIStore.getState().applyResult('t1', {
      task_id: 't1',
      branch: 'feat',
      status: 'error',
      error: 'gh run view failed (exit 4): gh auth login',
    });
    render(<CIStatusCard taskId="t1" branch="feat" />);
    expect(screen.getByRole('alert').textContent).toContain('gh auth login');
    expect(screen.getByText('error')).toBeTruthy();
  });

  it('shows the status lookup error when the watch row is gone', async () => {
    vi.mocked(getWatch).mockRejectedValue(new Error('unknown ci watch'));
    vi.mocked(ciStatus).mockRejectedValue(new Error('gh run list failed (exit 1): timeout'));
    render(<CIStatusCard taskId="t2" branch="feat" />);
    expect((await screen.findByRole('alert')).textContent).toContain('timeout');
  });

  it('shows the autofix error instead of dropping it', async () => {
    useSessionStore.setState({ currentSessionId: 's1' });
    useCIStore.getState().upsert('t3', {
      branch: 'feat',
      status: 'completed',
      conclusion: 'failure',
      failing: true,
      terminal: true,
    });
    vi.mocked(planAutofix).mockRejectedValue(new Error('no runs for branch feat'));
    render(<CIStatusCard taskId="t3" branch="feat" />);
    await act(async () => {
      fireEvent.click(screen.getByText('Autofix'));
    });
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Autofix failed: no runs for branch feat',
    );
  });
});
