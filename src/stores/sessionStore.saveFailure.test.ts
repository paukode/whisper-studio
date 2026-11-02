/**
 * A lasting save failure is retried every 30 s. The toast is keyed, so the
 * retries collapse into one card, and only the first failure of a run goes
 * to the notification bell. A save that lands ends the run, so a later
 * failure is reported again.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { persistMock } = vi.hoisted(() => ({ persistMock: vi.fn() }));
vi.mock('@/api/notify', () => ({
  persistToastNotification: persistMock,
}));
vi.mock('@/api/sessions', () => ({
  getSessions: vi.fn(),
  getSession: vi.fn(),
  createSession: vi.fn(),
  updateSession: vi.fn(),
  deleteSession: vi.fn(),
  bulkDeleteSessions: vi.fn(),
  setSessionFlags: vi.fn(),
  branchSession: vi.fn(),
}));

import * as sessionsApi from '@/api/sessions';
import { useSessionStore } from './sessionStore';
import { dropRuntime, getChatStore, useRuntimeIndex } from './sessionRuntimes';
import { useUIStore } from './uiStore';

async function failedSave(id: string) {
  await useSessionStore.getState().saveSession(id)?.catch(() => undefined);
  await Promise.resolve();
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  persistMock.mockClear();
  useUIStore.setState({ toasts: [], toastQueue: [] });
  vi.mocked(sessionsApi.createSession).mockResolvedValue({ ok: true });
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('a failing session save', () => {
  it('reaches the notification bell once per run of failures', async () => {
    const id = useSessionStore.getState().createSession();
    await Promise.resolve();
    getChatStore(id).getState().addMessage({ role: 'user', content: 'hello', timestamp: 't1' });

    vi.mocked(sessionsApi.updateSession).mockRejectedValue(new Error('HTTP 500'));
    await failedSave(id);
    await failedSave(id);

    expect(persistMock).toHaveBeenCalledTimes(1);
    const toasts = useUIStore.getState().toasts.filter((t) => t.key === `session-save-${id}`);
    expect(toasts).toHaveLength(1);
    expect(toasts[0].count).toBe(2);

    vi.mocked(sessionsApi.updateSession).mockResolvedValue({ ok: true });
    await useSessionStore.getState().saveSession(id);
    await Promise.resolve();

    getChatStore(id).getState().addMessage({ role: 'user', content: 'again', timestamp: 't2' });
    vi.mocked(sessionsApi.updateSession).mockRejectedValue(new Error('HTTP 500'));
    await failedSave(id);

    expect(persistMock).toHaveBeenCalledTimes(2);
  });
});
