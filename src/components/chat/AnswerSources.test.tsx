import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const get = vi.fn();
vi.mock('@/api/client', () => ({
  get: (...args: unknown[]) => get(...args),
  put: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
}));

import { AnswerSources } from './AnswerSources';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';

const SOURCES = {
  id: 'g1',
  sources: [
    {
      path: 'docs/contract.md',
      abs: '/ws/docs/contract.md',
      ws: '/ws',
      start_line: 12,
      end_line: 40,
      kind: 'keyword',
      entities: [],
      snippet: 'the payment terms are net 30',
    },
    {
      path: 'docs/summary.md',
      abs: '/ws/docs/summary.md',
      ws: '/ws',
      start_line: 1,
      end_line: 8,
      kind: 'semantic',
      entities: [],
      snippet: 'an overview of the engagement',
    },
    {
      path: 'docs/invoice.md',
      abs: '/ws/docs/invoice.md',
      ws: '/ws',
      start_line: 3,
      end_line: 9,
      kind: 'related',
      entities: ['Acme Corp'],
      snippet: 'invoice issued by Acme Corp',
    },
  ],
};

function renderChip(grounding: { searched: number; passages: number; id?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AnswerSources grounding={grounding} />
    </QueryClientProvider>,
  );
}

describe('AnswerSources', () => {
  beforeEach(() => {
    get.mockReset();
    get.mockResolvedValue(SOURCES);
    useSessionStore.setState({ currentSessionId: 'sess-1' });
    useUIStore.setState({ indexExplorer: null });
  });

  it('renders a static line when the turn persisted no sources (no id)', () => {
    renderChip({ searched: 2, passages: 0 });
    expect(screen.getByText(/Searched 2 indexed folders · no matches/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it('expands into labeled passages with file links and entity chips', async () => {
    renderChip({ searched: 1, passages: 3, id: 'g1' });
    fireEvent.click(screen.getByRole('button', { name: /Grounded in 1 indexed folder · 3 passages/ }));

    await waitFor(() => expect(screen.getByText('matched your words')).toBeInTheDocument());
    expect(get).toHaveBeenCalledWith('/api/sessions/sess-1/grounding/g1');
    expect(screen.getByText('similar meaning')).toBeInTheDocument();
    expect(screen.getByText('linked through shared entities:')).toBeInTheDocument();
    expect(screen.getByText('contract.md:12-40')).toBeInTheDocument();
    expect(screen.getByText(/the payment terms are net 30/)).toBeInTheDocument();

    // The entity chip deep-links into the index explorer, scoped to the
    // passage's own workspace.
    fireEvent.click(screen.getByRole('button', { name: 'Acme Corp' }));
    expect(useUIStore.getState().indexExplorer).toMatchObject({
      path: '/ws',
      target: { kind: 'entity', name: 'Acme Corp' },
    });

    // The file link opens the passage at its cited lines in the dock.
    const openFile = vi.spyOn(useDockStore.getState(), 'openFile');
    fireEvent.click(screen.getByText('contract.md:12-40'));
    expect(openFile).toHaveBeenCalledWith({
      path: '/ws/docs/contract.md',
      title: 'contract.md',
      startLine: 12,
      endLine: 40,
    });
  });

  it('degrades gracefully when the persisted row is gone (404)', async () => {
    get.mockRejectedValue(new Error('not found'));
    renderChip({ searched: 1, passages: 2, id: 'gone' });
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() =>
      expect(screen.getByText(/no longer available/)).toBeInTheDocument(),
    );
  });
});
