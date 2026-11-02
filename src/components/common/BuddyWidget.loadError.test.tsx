import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

const { getMock, postMock, putMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  putMock: vi.fn(),
}));
vi.mock('@/api/client', () => ({ get: getMock, post: postMock, put: putMock }));

import { BuddyWidget } from './BuddyWidget';
import { useUIStore } from '@/stores/uiStore';
import { ApiError } from '@/types/api';

function serveBuddyError(err: Error): void {
  getMock.mockReset().mockImplementation(async (url: string) => {
    if (url === '/api/feature-flags') return { companion: { enabled: true } };
    if (url === '/api/buddy') throw err;
    throw new Error(`unexpected ${url}`);
  });
}

const toastMessages = () => useUIStore.getState().toasts.map((t) => t.message);

/** A companion the server cannot load stays hidden, never re-hatched, and the
 *  server's reason is shown instead of a silent disappearance. */
describe('BuddyWidget load error', () => {
  beforeEach(() => {
    localStorage.clear();
    postMock.mockReset();
    useUIStore.setState({ toasts: [] });
  });

  it('shows the server reason and does not hatch', async () => {
    serveBuddyError(new ApiError(500, 'The companion file /x/buddy.json cannot be read: bad', '/api/buddy', 'GET'));
    render(<BuddyWidget />);
    await waitFor(() =>
      expect(toastMessages()).toContain('Companion not shown: The companion file /x/buddy.json cannot be read: bad'),
    );
    expect(postMock).not.toHaveBeenCalledWith('/api/buddy/hatch', expect.anything());
  });

  it('adds no toast when the backend is unreachable', async () => {
    serveBuddyError(new ApiError(0, "Can't reach the server.", '/api/buddy', 'GET'));
    render(<BuddyWidget />);
    await waitFor(() => expect(getMock).toHaveBeenCalledWith('/api/buddy'));
    expect(toastMessages()).toEqual([]);
  });
});
