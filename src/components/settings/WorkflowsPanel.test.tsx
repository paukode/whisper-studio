/**
 * The panel has to show RUNS, not just saved scripts.
 *
 * It used to query `listSaved` alone, so a user who had run workflows still
 * saw "no saved workflows yet" — the complaint that started this: "I don't see
 * any workflow in the settings". Runs and saved scripts are different stores;
 * launching writes a run row and never touches the saved library.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { WorkflowsPanel } from './WorkflowsPanel';

const RUN = {
  run_id: 'abc123',
  name: 'nightly-review',
  status: 'done' as const,
  agents_spawned: 12,
  tokens_in: 400,
  tokens_out: 900,
  cost_usd: 1.234,
  cap_reached: false,
  error: '',
  started_at: '2026-08-13T10:00:00Z',
};

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowsPanel />
    </QueryClientProvider>,
  );
}

/** Route each query by URL: the panel reads two independent endpoints. */
function mockApi({ runs = [] as unknown[], saved = [] as unknown[] } = {}) {
  getMock.mockImplementation((url: string) =>
    url.startsWith('/api/workflows/runs')
      ? Promise.resolve({ runs })
      : Promise.resolve({ saved }),
  );
}

describe('WorkflowsPanel', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it('lists runs with their agent count and cost', async () => {
    mockApi({ runs: [RUN] });
    renderPanel();

    await waitFor(() => expect(screen.getByText('nightly-review')).toBeTruthy());
    expect(screen.getByText('done')).toBeTruthy();
    expect(screen.getByText(/12 agents/)).toBeTruthy();
    expect(screen.getByText(/\$1\.23/)).toBeTruthy();
  });

  it('points at Ultracode when nothing has run yet', async () => {
    mockApi();
    renderPanel();

    await waitFor(() => expect(screen.getByText(/No workflow runs yet/)).toBeTruthy());
    expect(screen.getByText('Ultracode')).toBeTruthy();
  });

  it('shows runs even when no workflow has been saved', async () => {
    // The exact reported state: runs exist, the saved library is empty. The
    // panel must not read as "you have no workflows".
    mockApi({ runs: [RUN], saved: [] });
    renderPanel();

    await waitFor(() => expect(screen.getByText('nightly-review')).toBeTruthy());
    expect(screen.getByText(/No saved workflows yet/)).toBeTruthy();
  });

  it('still lists saved scripts alongside runs', async () => {
    mockApi({
      runs: [RUN],
      saved: [{ name: 'my-flow', description: 'does a thing', phases: ['a'], trusted: true }],
    });
    renderPanel();

    await waitFor(() => expect(screen.getByText('my-flow')).toBeTruthy());
    expect(screen.getByText(/does a thing/)).toBeTruthy();
    // "trusted" also appears in the explanatory hint, hence getAllByText.
    expect(screen.getAllByText('trusted').length).toBeGreaterThan(0);
    expect(screen.getByText('nightly-review')).toBeTruthy();
  });
});
