import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query';
import { GitDiffViewer } from './GitDiffViewer';
import { ApiError } from '@/types/api';

const getGitFileDiff = vi.fn();
vi.mock('@/api/git', () => ({
  getGitFileDiff: (path: string) => getGitFileDiff(path),
}));

const DIFF = {
  path: 'a.txt',
  mode: 'full' as const,
  status: 'modified' as const,
  old: 'one\ntwo\n',
  new: 'one\nTWO\n',
  hunks: [],
  added: 1,
  removed: 1,
  truncated: false,
  error: null,
};

function renderViewer(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <GitDiffViewer path="a.txt" onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  onlineManager.setOnline(true);
  getGitFileDiff.mockReset();
});

describe('GitDiffViewer failure states', () => {
  it('surfaces the real error instead of a generic line', async () => {
    getGitFileDiff.mockRejectedValue(
      new ApiError(0, "Can't reach the server.", '/api/git/file-diff', 'GET'),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, networkMode: 'always' } },
    });

    renderViewer(client);

    expect(await screen.findByText("Can't reach the server.")).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('explains a paused query rather than rendering an empty dialog', async () => {
    // A query parked in `paused` reports isPending true, isFetching false,
    // no error and no data. Every render branch used to be false for that
    // combination, so the dialog body came up blank with nothing to act on
    // and no way to recover short of closing and reopening it.
    onlineManager.setOnline(false);
    getGitFileDiff.mockRejectedValue(new Error('never called'));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, networkMode: 'online' } },
    });

    const { container } = renderViewer(client);

    await waitFor(() => {
      const query = client.getQueryCache().find({ queryKey: ['git-file-diff', 'a.txt'] });
      expect(query?.state.fetchStatus).toBe('paused');
    });

    expect(container.querySelector('.git-diff-body')?.innerHTML).not.toBe('');
    expect(screen.getByText(/Lost the connection to the backend/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('retry recovers a paused query, which refetch alone cannot', async () => {
    // Query.fetch hands back the parked retryer promise when there is no
    // cached data, so refetch() is a no-op in exactly this case; the button
    // has to reset the query to drop the stuck retryer.
    onlineManager.setOnline(false);
    getGitFileDiff
      .mockRejectedValueOnce(new ApiError(0, 'Load failed', '/api/git/file-diff', 'GET'))
      .mockResolvedValue(DIFF);
    const client = new QueryClient({
      // retryDelay 0 keeps the assertion about the pause, not about backoff.
      defaultOptions: { queries: { retry: 1, retryDelay: 0, networkMode: 'online' } },
    });

    const { container } = renderViewer(client);

    await waitFor(() => {
      const query = client.getQueryCache().find({ queryKey: ['git-file-diff', 'a.txt'] });
      expect(query?.state.fetchStatus).toBe('paused');
    });

    onlineManager.setOnline(true);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    // The diff renders, so the reset dropped the parked retryer and refetched.
    await waitFor(() =>
      expect(container.querySelector('.git-diff-grid')).toBeTruthy(),
    );
    expect(container.textContent).toContain('TWO');
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('renders a server-reported diff error with its own message', async () => {
    getGitFileDiff.mockResolvedValue({ ...DIFF, mode: 'error', error: 'File not found.' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    renderViewer(client);

    expect(await screen.findByText('File not found.')).toBeTruthy();
  });
});
