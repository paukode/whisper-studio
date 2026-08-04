import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NotificationsBell } from './NotificationsBell';

const LONG_BODY = Array.from({ length: 12 }, (_, i) => `line ${i + 1} of a long report`).join(
  '\n',
);

const ROWS = {
  notifications: [
    {
      id: 1,
      session_id: 'sess-1',
      source: 'model-download',
      title: 'Download failed',
      message: LONG_BODY,
      status: 'error',
      created_at: new Date().toISOString(),
      read_at: null,
      count: 3,
    },
    {
      id: 2,
      session_id: '',
      source: 'app',
      title: '',
      message: 'short note',
      status: 'success',
      created_at: new Date().toISOString(),
      read_at: '2026-08-04T00:00:00Z',
      count: 1,
    },
  ],
};

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(() => Promise.resolve({})),
}));
vi.mock('@/api/client', () => ({ get: getMock, post: postMock }));

const switchSession = vi.fn();
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: { getState: () => ({ switchSession }) },
}));

function renderBell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NotificationsBell />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockClear();
  switchSession.mockClear();
  getMock.mockImplementation((url: string) => {
    if (url.startsWith('/api/notifications/unread-count')) return Promise.resolve({ unread: 1 });
    return Promise.resolve(ROWS);
  });
});

describe('NotificationsBell', () => {
  it('lists notifications with source, severity, and repeat count', async () => {
    renderBell();
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }));
    await waitFor(() => expect(screen.getByText('Download failed')).toBeInTheDocument());
    expect(screen.getByText('model-download')).toBeInTheDocument();
    expect(screen.getByTitle('Repeated 3 times')).toHaveTextContent('×3');
    expect(document.querySelector('.notif-dot-error')).not.toBeNull();
  });

  it('clamps long bodies and expands in place on click, marking the row read', async () => {
    renderBell();
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }));
    await waitFor(() => expect(screen.getByText('Download failed')).toBeInTheDocument());

    const row = screen.getByText('Download failed').closest('.notif-row');
    expect(row).not.toBeNull();
    // Collapsed by default: the clamped class stays and the expanded marker is absent.
    expect(row!.classList.contains('notif-row-expanded')).toBe(false);

    const body = screen.getByText('Download failed').closest('button');
    fireEvent.click(body!);
    expect(body).toHaveAttribute('aria-expanded', 'true');
    expect(row!.classList.contains('notif-row-expanded')).toBe(true);
    // Opening the details marks the unread row read.
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith('/api/notifications/read', { ids: [1] }),
    );
    // Clicking again collapses.
    fireEvent.click(body!);
    expect(row!.classList.contains('notif-row-expanded')).toBe(false);

    // A session-linked row offers "Open session" once expanded (this closes
    // the panel and switches to the originating session).
    fireEvent.click(body!);
    fireEvent.click(screen.getByText('Open session'));
    expect(switchSession).toHaveBeenCalledWith('sess-1');
  });

  it('does not mark an already-read row read again when expanded', async () => {
    renderBell();
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }));
    await waitFor(() => expect(screen.getByText('short note')).toBeInTheDocument());
    fireEvent.click(screen.getByText('short note').closest('button')!);
    expect(postMock).not.toHaveBeenCalledWith('/api/notifications/read', { ids: [2] });
  });
});
