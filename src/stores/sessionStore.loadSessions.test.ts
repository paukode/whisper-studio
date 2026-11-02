/**
 * The session list must match the summary schema: a payload in another
 * shape is a visible error, never rows rebuilt from whatever arrived.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/sessions', () => ({ getSessions: vi.fn() }));

import * as sessionsApi from '@/api/sessions';
import { useSessionStore } from './sessionStore';
import { useUIStore } from './uiStore';

afterEach(() => {
  vi.clearAllMocks();
  useSessionStore.setState({ sessions: [] });
  useUIStore.setState({ toasts: [] });
});

const row = {
  id: 's1',
  title: 'Notes',
  date: '2026-11-02T08:00:00Z',
  segmentCount: 3,
  chatCount: 4,
  workspacePath: '/Users/demo/proj',
  pinned: true,
  archived: false,
};

describe('loadSessions', () => {
  it('keeps every summary field the server sends', async () => {
    vi.mocked(sessionsApi.getSessions).mockResolvedValue([row]);
    await useSessionStore.getState().loadSessions();
    expect(useSessionStore.getState().sessions).toEqual([row]);
  });

  it('reports a list in another shape and keeps the old list', async () => {
    useSessionStore.setState({ sessions: [row] });
    vi.mocked(sessionsApi.getSessions).mockResolvedValue({ sessions: [{ id: 's2' }] });
    await useSessionStore.getState().loadSessions();
    expect(useSessionStore.getState().sessions).toEqual([row]);
    expect(useUIStore.getState().toasts[0]?.message).toMatch(/^Failed to load sessions: /);
  });
});
