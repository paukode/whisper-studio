import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryViewerModal } from './MemoryViewerModal';

const LISTING = {
  workspace_connected: true,
  tiers: [
    {
      scope: 'global',
      index: '- [User Role](user_role.md): payments engineer',
      files: [
        {
          filename: 'user_role.md',
          name: 'User Role',
          description: 'payments engineer',
          type: 'user',
          scope: 'global',
          mtime: '2026-07-11T00:00:00+00:00',
          size: 120,
        },
      ],
    },
    {
      scope: 'project',
      index: '',
      files: [
        {
          filename: 'goals.md',
          name: 'Q3 Goals',
          description: 'ship the viewer',
          type: 'project',
          scope: 'project',
          mtime: '2026-07-11T00:00:00+00:00',
          size: 80,
        },
      ],
    },
  ],
};

vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) => {
    if (url === '/api/memory') return Promise.resolve(LISTING);
    return Promise.resolve({ content: '---\nname: X\n---\n\nbody' });
  }),
  put: vi.fn(() => Promise.resolve({ saved: true })),
  post: vi.fn(() => Promise.resolve({ promoted: true })),
  del: vi.fn(() => Promise.resolve({ deleted: true })),
}));

function renderModal(props: Partial<React.ComponentProps<typeof MemoryViewerModal>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryViewerModal isOpen onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  );
}

describe('MemoryViewerModal — two-tier browser', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders nothing when closed', () => {
    const qc = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryViewerModal isOpen={false} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it('lists global tier files by default', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText('User Role')).toBeTruthy());
    expect(screen.getByText('payments engineer')).toBeTruthy();
    // Project file not shown on the global tab
    expect(screen.queryByText('Q3 Goals')).toBeNull();
  });

  it('switches to the project tab and offers promote there', async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText('User Role')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /project/i }));
    await waitFor(() => expect(screen.getByText('Q3 Goals')).toBeTruthy());

    // Open the file: promote button appears for project scope
    fireEvent.click(screen.getByText('Q3 Goals'));
    await waitFor(() => expect(screen.getByRole('button', { name: /promote to global/i })).toBeTruthy());
  });

  it('drops the open file when the modal closes, so reopen shows the list', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <MemoryViewerModal isOpen onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText('User Role')).toBeTruthy());
    fireEvent.click(screen.getByText('User Role'));
    await waitFor(() => expect(screen.getByRole('button', { name: /back to list/i })).toBeTruthy());

    // Close and reopen: the stale editor (and its editContent) must be gone,
    // otherwise Save could clobber a file updated while the modal was closed.
    rerender(
      <QueryClientProvider client={qc}>
        <MemoryViewerModal isOpen={false} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    rerender(
      <QueryClientProvider client={qc}>
        <MemoryViewerModal isOpen onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText('User Role')).toBeTruthy());
    expect(screen.queryByRole('button', { name: /back to list/i })).toBeNull();
  });

  it('disables the project tab when no workspace is connected', async () => {
    const { get } = await import('@/api/client');
    (get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/memory') {
        return Promise.resolve({ workspace_connected: false, tiers: [LISTING.tiers[0]] });
      }
      return Promise.resolve({ content: '' });
    });
    renderModal();
    await waitFor(() => expect(screen.getByText('User Role')).toBeTruthy());
    const projectTab = screen.getByRole('button', { name: /project/i }) as HTMLButtonElement;
    expect(projectTab.disabled).toBe(true);
  });
});
