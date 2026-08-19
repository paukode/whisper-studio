import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const get = vi.fn();
vi.mock('@/api/client', () => ({
  get: (...args: unknown[]) => get(...args),
  put: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

import { IndexExplorer } from './IndexExplorer';
import { useUIStore } from '@/stores/uiStore';

const OVERVIEW = {
  files: 5,
  passages: 12,
  entities: 4,
  last_indexed_at: null,
  cards: [
    {
      id: 0,
      names: ['Northwind Bank', 'Halloway Ledger'],
      files: 3,
      top_files: [{ path: 'onboarding.md', name: 'onboarding.md' }],
    },
  ],
  stands_out: [{ name: 'Northwind Bank', label: 'organization', files: 3 }],
  most_connected: [{ path: 'onboarding.md', name: 'onboarding.md', links: 4 }],
  root: '/fake/ws',
};

const ENTITIES = {
  groups: [
    {
      key: 'organizations',
      title: 'Organizations',
      entities: [
        { name: 'Northwind Bank', label: 'organization', salience: 0.9, files: 3, mentions: 7, description: '' },
      ],
    },
  ],
  total: 1,
  hidden: 2,
  root: '/fake/ws',
};

const ENTITY_PAGE = {
  found: true,
  name: 'Northwind Bank',
  label: 'organization',
  description: 'The client bank.',
  facts: [
    {
      predicate: 'customer_of',
      other: 'Idero Consulting',
      direction: 'out',
      score: 0.8,
      sources: 3,
      files: 3,
      cite: { path: 'runbook.md', name: 'runbook.md', start_line: 41, end_line: 41, evidence: 'quoted line' },
    },
  ],
  mentioned_in: [{ path: 'onboarding.md', name: 'onboarding.md', passages: 5 }],
  related: [{ name: 'Halloway Ledger', label: 'product', shared: 4 }],
  root: '/fake/ws',
};

function respond(url: string): unknown {
  if (url.includes('/explore/overview')) return OVERVIEW;
  if (url.includes('/explore/entities')) return ENTITIES;
  if (url.includes('/explore/entity')) return ENTITY_PAGE;
  throw new Error(`unexpected GET ${url}`);
}

function renderExplorer() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <IndexExplorer />
    </QueryClientProvider>,
  );
}

describe('IndexExplorer', () => {
  beforeEach(() => {
    get.mockReset();
    get.mockImplementation((url: string) => Promise.resolve(respond(url)));
    localStorage.clear();
    useUIStore.setState({ indexExplorer: null });
  });

  it('renders nothing while closed', () => {
    renderExplorer();
    expect(screen.queryByText(/Explore index/)).toBeNull();
  });

  it('opens on the overview with stats, topic cards, and the browse pane', async () => {
    renderExplorer();
    useUIStore.getState().openIndexExplorer('/fake/ws');
    await waitFor(() => expect(screen.getByText(/5 files/)).toBeInTheDocument());
    // Topic card claims co-mention, not aboutness.
    expect(screen.getByText('Northwind Bank · Halloway Ledger')).toBeInTheDocument();
    expect(screen.getByText(/3 files mention these together/)).toBeInTheDocument();
    // Browse pane row with an honest count + the hidden-names toggle.
    expect(screen.getByText('in 3 files')).toBeInTheDocument();
    expect(screen.getByText('Show 2 low-signal names')).toBeInTheDocument();
    // First-run note is visible until dismissed.
    expect(screen.getByText(/Click a name to see everything about it/)).toBeInTheDocument();
  });

  it('navigates to an entity page and renders facts as sentences with evidence', async () => {
    renderExplorer();
    useUIStore.getState().openIndexExplorer('/fake/ws', {
      kind: 'entity',
      name: 'Northwind Bank',
      label: 'organization',
    });
    await waitFor(() =>
      expect(screen.getByText('Northwind Bank is a customer of Idero Consulting.')).toBeInTheDocument(),
    );
    expect(screen.getByText('confirmed in 3 files')).toBeInTheDocument();
    expect(screen.getByText(/quoted line/)).toBeInTheDocument();
    expect(screen.getByText('runbook.md, line 41')).toBeInTheDocument();
    expect(screen.getByText('The client bank.')).toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    renderExplorer();
    useUIStore.getState().openIndexExplorer('/fake/ws');
    await waitFor(() => expect(screen.getByText(/5 files/)).toBeInTheDocument());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(useUIStore.getState().indexExplorer).toBeNull());
  });
});
