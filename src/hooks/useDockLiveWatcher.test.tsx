/**
 * The live-preview poller used to hit /api/preview/sessions every 2s per
 * window, forever, whether or not a preview existed or the window was even
 * visible: 75% of a 221K-line backend log. It now polls fast only while a
 * preview is live or the dock is open, slowly otherwise, and not at all
 * while the window is hidden.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { ACTIVE_POLL_MS, IDLE_POLL_MS, useDockLiveWatcher } from './useDockLiveWatcher';
import { listPreviewSessions } from '@/api/preview';
import { useDockStore } from '@/stores/dockStore';

vi.mock('@/api/preview', () => ({ listPreviewSessions: vi.fn() }));

const list = listPreviewSessions as unknown as ReturnType<typeof vi.fn>;

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
}

describe('useDockLiveWatcher', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    list.mockReset();
    list.mockResolvedValue([]);
    useDockStore.setState({ open: false, liveSession: null });
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    setHidden(false);
  });

  it('idle (no preview, dock closed): one poll on mount, then the slow cadence', async () => {
    renderHook(() => useDockLiveWatcher());
    await vi.advanceTimersByTimeAsync(0);
    expect(list).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(IDLE_POLL_MS - 1000);
    expect(list).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1100);
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('active (a preview is live): polls on the fast cadence', async () => {
    list.mockResolvedValue([{ id: 'p1', url: 'http://localhost:3000', port: 3000, process_alive: true }]);
    renderHook(() => useDockLiveWatcher());
    await vi.advanceTimersByTimeAsync(0);
    expect(list).toHaveBeenCalledTimes(1);
    expect(useDockStore.getState().liveSession?.name).toBe('p1');

    await vi.advanceTimersByTimeAsync(ACTIVE_POLL_MS + 100);
    expect(list).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(ACTIVE_POLL_MS);
    expect(list).toHaveBeenCalledTimes(3);
  });

  it('hidden window: no polls at all, one immediately on becoming visible', async () => {
    setHidden(true);
    renderHook(() => useDockLiveWatcher());
    await vi.advanceTimersByTimeAsync(IDLE_POLL_MS * 3);
    expect(list).toHaveBeenCalledTimes(0);

    setHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('stops polling on unmount', async () => {
    const { unmount } = renderHook(() => useDockLiveWatcher());
    await vi.advanceTimersByTimeAsync(0);
    expect(list).toHaveBeenCalledTimes(1);
    unmount();
    await vi.advanceTimersByTimeAsync(IDLE_POLL_MS * 2);
    expect(list).toHaveBeenCalledTimes(1);
  });
});
