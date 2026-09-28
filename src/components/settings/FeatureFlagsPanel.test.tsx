import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('@/api/client', () => api);

import { FeatureFlagsPanel } from './FeatureFlagsPanel';

const REASON = 'Off in Local mode: the rewrite runs on a cloud model.';

function flag(extra: Record<string, unknown>) {
  return { enabled: true, default: false, description: 'desc', category: 'chat', source: 'config', ...extra };
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <FeatureFlagsPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('FeatureFlagsPanel', () => {
  it('shows why a switched-on flag has no effect in the active mode', async () => {
    api.get.mockResolvedValue({
      rag_query_rewrite: flag({ inactive_reason: REASON }),
      rag_hybrid_search: flag({ inactive_reason: null }),
    });
    renderPanel();
    expect(await screen.findByText(REASON)).toBeInTheDocument();
    // Only the inactive flag carries a note; the switch still shows the setting.
    expect(screen.getAllByRole('note')).toHaveLength(1);
    expect(screen.getByLabelText('Toggle rag_query_rewrite')).toBeChecked();
  });

  it('says what the active mode leaves out of a flag that stays on', async () => {
    const note = 'Local mode recalls saved memories but records no new ones.';
    api.get.mockResolvedValue({ auto_memory: flag({ category: 'memory', local_mode_note: note }) });
    renderPanel();
    expect(await screen.findByText(note)).toBeInTheDocument();
    expect(screen.getByLabelText('Toggle auto_memory')).toBeChecked();
  });
});
