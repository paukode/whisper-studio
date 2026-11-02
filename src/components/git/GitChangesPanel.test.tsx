/**
 * A failed git read is shown in the Git Changes panel with git's message,
 * never as an empty list that looks like a clean tree.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiError } from '@/types/api';

const h = vi.hoisted(() => ({ getGitChanges: vi.fn() }));

vi.mock('@/api/git', () => ({
  getGitChanges: h.getGitChanges,
  gitRestoreFile: vi.fn(),
}));
vi.mock('./GitDiffViewer', () => ({ GitDiffViewer: () => null }));

import { GitChangesPanel } from './GitChangesPanel';

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe('GitChangesPanel', () => {
  beforeEach(() => {
    h.getGitChanges.mockReset();
  });

  it('shows the git failure in place of the file list', async () => {
    h.getGitChanges.mockRejectedValue(
      new ApiError(500, 'git status failed: fatal: index file corrupt', '/api/git/changes', 'GET'),
    );
    const { container } = render(<GitChangesPanel onFileOpen={() => {}} />, { wrapper });

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('git status failed: fatal: index file corrupt');
    expect(screen.queryByText('No changes')).toBeNull();
    const panel = container.querySelector('.ws-git-changes') as HTMLElement;
    expect(panel.style.display).not.toBe('none');
  });

  it('stays hidden when the workspace is not a git repository', async () => {
    h.getGitChanges.mockRejectedValue(
      new ApiError(400, 'Workspace is not a git repository.', '/api/git/changes', 'GET'),
    );
    const { container } = render(<GitChangesPanel onFileOpen={() => {}} />, { wrapper });

    await waitFor(() => expect(h.getGitChanges).toHaveBeenCalled());
    await waitFor(() => {
      const panel = container.querySelector('.ws-git-changes') as HTMLElement;
      expect(panel.style.display).toBe('none');
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
