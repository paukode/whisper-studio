/**
 * branchSession must flush-save the source session BEFORE asking the
 * backend to branch it. The server branch copies the persisted session,
 * so the save has to land first — otherwise the branch inherits stale
 * state. saveSession returns its update promise for exactly this reason;
 * branchSession awaits it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/types/session';

// sessionStore imports `* as sessionsApi from '@/api/sessions'`. Mock the
// whole module so no real network calls fire and we can observe ordering.
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

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function makeSession(id: string): Session {
  const now = new Date().toISOString();
  return {
    id,
    title: `Session ${id}`,
    customTitle: false,
    generatedTitle: false,
    createdAt: now,
    updatedAt: now,
    segments: [],
    chatHistory: [],
    speakerNames: {},
  };
}

beforeEach(() => {
  vi.mocked(sessionsApi.getSessions).mockResolvedValue([]);
  vi.mocked(sessionsApi.getSession).mockResolvedValue(makeSession('new-id'));
  vi.mocked(sessionsApi.branchSession).mockResolvedValue({
    new_session_id: 'new-id',
    name: 'Branch 1',
  });
  useSessionStore.setState({
    currentSessionId: null,
    liveSessions: { 'src-id': makeSession('src-id') },
    sessions: [],
    isLoading: false,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('sessionStore.branchSession', () => {
  it('awaits saveSession before calling branchSession on the server', async () => {
    // Hold the save open: updateSession stays pending until we resolve it.
    let resolveUpdate!: (result: { ok: boolean }) => void;
    const updatePromise = new Promise<{ ok: boolean }>((resolve) => {
      resolveUpdate = resolve;
    });
    vi.mocked(sessionsApi.updateSession).mockReturnValue(updatePromise);

    const branchPromise = useSessionStore.getState().branchSession('src-id');
    await flush();

    // The flush-save has been issued, but the branch must WAIT for it.
    expect(sessionsApi.updateSession).toHaveBeenCalledWith('src-id', expect.any(Object));
    expect(sessionsApi.branchSession).not.toHaveBeenCalled();

    // Once the save lands, the branch proceeds.
    resolveUpdate({ ok: true });
    await branchPromise;

    expect(sessionsApi.branchSession).toHaveBeenCalledWith('src-id');

    // And it happened strictly after the save was invoked.
    const updateOrder = vi.mocked(sessionsApi.updateSession).mock.invocationCallOrder[0];
    const branchOrder = vi.mocked(sessionsApi.branchSession).mock.invocationCallOrder[0];
    expect(updateOrder).toBeLessThan(branchOrder);
  });

  it('saveSession returns the underlying update promise', async () => {
    const updated = { ok: true };
    vi.mocked(sessionsApi.updateSession).mockResolvedValue(updated);

    const result = await useSessionStore.getState().saveSession('src-id');

    expect(sessionsApi.updateSession).toHaveBeenCalledWith('src-id', expect.any(Object));
    expect(result).toBe(updated);
  });
});
