/**
 * The context menu's search and history actions show the server's reason
 * (git's or rg's message) when they fail, not a generic "not available".
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiError } from '@/types/api';
import { useUIStore } from '@/stores/uiStore';

const h = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('@/api/client', () => ({ get: h.get, post: vi.fn() }));

import { WorkspaceContextMenu } from './WorkspaceContextMenu';

function openMenu() {
  render(
    <WorkspaceContextMenu
      state={{ x: 0, y: 0, path: 'src/a.ts', type: 'file' }}
      onClose={() => {}}
      onFileSelect={() => {}}
    />,
  );
}

describe('WorkspaceContextMenu errors', () => {
  const addToast = vi.fn();

  beforeEach(() => {
    h.get.mockReset();
    addToast.mockReset();
    useUIStore.setState({ addToast } as never);
  });

  it('shows the rg reason when Find references fails', async () => {
    h.get.mockRejectedValue(
      new ApiError(500, 'Search failed: ripgrep (rg) not found', '/api/workspace/grep', 'GET'),
    );
    openMenu();
    fireEvent.click(screen.getByText('Find references to this file'));

    await waitFor(() => expect(addToast).toHaveBeenCalled());
    expect(addToast.mock.calls[0][0]).toMatchObject({
      type: 'error',
      message: 'Find references failed: Search failed: ripgrep (rg) not found',
    });
  });

  it('shows git\'s message when Open Timeline fails', async () => {
    h.get.mockRejectedValue(
      new ApiError(500, 'fatal: not a git repository', '/api/workspace/file-history', 'GET'),
    );
    openMenu();
    fireEvent.click(screen.getByText('Open Timeline'));

    await waitFor(() => expect(addToast).toHaveBeenCalled());
    expect(addToast.mock.calls[0][0]).toMatchObject({
      type: 'error',
      message: 'Timeline failed: fatal: not a git repository',
    });
  });

  it('lists the hits the grep route returns', async () => {
    const pushDialog = vi.fn();
    useUIStore.setState({ addToast, pushDialog } as never);
    h.get.mockResolvedValue({ results: [{ path: 'src/b.ts', line: 3, content: '  import a  ' }] });
    openMenu();
    fireEvent.click(screen.getByText('Find references to this file'));

    await waitFor(() => expect(pushDialog).toHaveBeenCalled());
    expect(addToast).not.toHaveBeenCalled();
    render(<>{pushDialog.mock.calls[0][0].body}</>);
    expect(screen.getByText('src/b.ts:3')).toBeTruthy();
    expect(screen.getByText('import a')).toBeTruthy();
  });
});
