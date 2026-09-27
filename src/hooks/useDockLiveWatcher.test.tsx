/**
 * The live-preview poller used to hit /api/preview/sessions every 2s per
 * window, forever, whether or not a preview existed or the window was even
 * visible: 75% of a 221K-line backend log. It now polls fast only while a
 * preview is live or the dock is open, slowly otherwise, and not at all
 * while the window is hidden.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { ACTIVE_POLL_MS, IDLE_POLL_MS, pickLiveSession, useDockLiveWatcher } from './useDockLiveWatcher';
import { listPreviewSessions, type PreviewSession } from '@/api/preview';
import { useDockStore } from '@/stores/dockStore';
import { useSessionStore } from '@/stores/sessionStore';

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
    useSessionStore.setState({ currentSessionId: 'chat-a' });
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
    list.mockResolvedValue([
      { id: 'p1', url: 'http://localhost:3000', port: 3000, process_alive: true, owner: 'chat-a' },
    ]);
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

function row(id: string, owner: string, extra: Partial<PreviewSession> = {}): PreviewSession {
  return {
    id,
    url: null,
    port: null,
    process_alive: true,
    browser_started: false,
    created_at: 0,
    owner,
    ...extra,
  };
}

// Field report: one chat built an app on a preview while another debugged on
// its own; the single Live pane followed the latest server of ANY chat, so its
// Stop could land on the other chat's app. The pane follows only the chat on
// screen now.
describe('pickLiveSession', () => {
  it("picks the chat's own latest alive server, never a later one from another chat", () => {
    const list = [row('app', 'chat-a'), row('debug', 'chat-b')];
    expect(pickLiveSession(list, 'chat-a')?.name).toBe('app');
    expect(pickLiveSession(list, 'chat-b')?.name).toBe('debug');
  });

  it("shows nothing for a chat that started no preview, whatever else runs", () => {
    expect(pickLiveSession([row('app', 'chat-a')], 'chat-c')).toBeNull();
    expect(pickLiveSession([row('app', 'chat-a')], null)).toBeNull();
  });

  it('skips a crashed server of the same chat', () => {
    const list = [row('old', 'chat-a'), row('new', 'chat-a', { process_alive: false })];
    expect(pickLiveSession(list, 'chat-a')?.name).toBe('old');
  });
});

describe('useDockLiveWatcher chat scoping', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    list.mockReset();
    useDockStore.setState({ open: false, liveSession: null });
    useSessionStore.setState({ currentSessionId: 'chat-a' });
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('switching chats re-points the pane at once, without waiting for the next poll', async () => {
    list.mockResolvedValue([row('app', 'chat-a'), row('debug', 'chat-b')]);
    renderHook(() => useDockLiveWatcher());
    await vi.advanceTimersByTimeAsync(0);
    expect(useDockStore.getState().liveSession).toMatchObject({ name: 'app', owner: 'chat-a' });

    useSessionStore.setState({ currentSessionId: 'chat-b' });
    await vi.advanceTimersByTimeAsync(0);
    expect(useDockStore.getState().liveSession).toMatchObject({ name: 'debug', owner: 'chat-b' });

    useSessionStore.setState({ currentSessionId: 'chat-c' });
    await vi.advanceTimersByTimeAsync(0);
    expect(useDockStore.getState().liveSession).toBeNull();
  });
});
