/**
 * The panel manages the SAVED library only.
 *
 * It once also listed runs (added in #60 after "I don't see any workflow in
 * the settings", removed later by the same user's request): a run's home is
 * the inline chat card that launched it, and approval happens there by
 * permission mode. Settings keeps only the durable bits — the trust tick and
 * deletion — so these tests pin that the runs endpoint is never fetched.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { WorkflowsPanel } from './WorkflowsPanel';

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowsPanel />
    </QueryClientProvider>,
  );
}

function mockApi({ saved = [] as unknown[] } = {}) {
  getMock.mockImplementation(() => Promise.resolve({ saved }));
}

describe('WorkflowsPanel', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it('lists saved scripts with their trust state', async () => {
    mockApi({
      saved: [{ name: 'my-flow', description: 'does a thing', phases: ['a'], trusted: true }],
    });
    renderPanel();

    await waitFor(() => expect(screen.getByText('my-flow')).toBeTruthy());
    expect(screen.getByText(/does a thing/)).toBeTruthy();
    // "trusted" also appears in the explanatory hint, hence getAllByText.
    expect(screen.getAllByText('trusted').length).toBeGreaterThan(0);
  });

  it('offers Trust only for untrusted scripts', async () => {
    mockApi({
      saved: [
        { name: 'pinned', description: '', phases: [], trusted: true },
        { name: 'loose', description: '', phases: [], trusted: false },
      ],
    });
    renderPanel();

    await waitFor(() => expect(screen.getByText('loose')).toBeTruthy());
    expect(screen.getAllByRole('button', { name: 'Trust' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
  });

  it('shows the empty state when nothing is saved', async () => {
    mockApi();
    renderPanel();

    await waitFor(() => expect(screen.getByText(/No saved workflows yet/)).toBeTruthy());
  });

  it('never fetches the runs endpoint', async () => {
    mockApi();
    renderPanel();

    await waitFor(() => expect(getMock).toHaveBeenCalled());
    const urls = getMock.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/runs'))).toBe(false);
  });
});
