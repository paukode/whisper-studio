/**
 * When background commands a session started have finished, this window
 * starts a turn for that session: at once when it is idle, once it is free
 * when it is busy, once a slot frees when the active-session ceiling is
 * reached, and never for a session it has no hydrated runtime for.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  busy: new Set<string>(),
  hydrated: new Set<string>(),
  active: 0,
}));
const sendChatTurn = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));

vi.mock('@/stores/sessionRuntimes', () => ({
  isSessionBusy: (sid: string) => state.busy.has(sid),
  hasHydratedRuntime: (sid: string) => state.hydrated.has(sid),
  countActiveSessions: () => state.active,
}));
vi.mock('../useChatStream', () => ({ MAX_ACTIVE_SESSIONS: 3, sendChatTurn }));

import { _resetPendingWakes, flushPendingWakes, requestWake } from './backgroundWake';

/** Let the lazy load of useChatStream (and its .then) run. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const wakeOf = (sid: string, ids: string[]) => [
  '',
  { sessionId: sid, hideUserMessage: true, runtimeWake: { task_ids: ids } },
];

beforeEach(() => {
  _resetPendingWakes();
  sendChatTurn.mockClear();
  state.busy.clear();
  state.hydrated = new Set(['s1']);
  state.active = 0;
});

describe('background wake', () => {
  it('starts a turn at once for an idle session', async () => {
    requestWake('s1', ['t1']);
    await settle();
    expect(sendChatTurn.mock.calls).toEqual([wakeOf('s1', ['t1'])]);
  });

  it('waits while the session is busy, then wakes once for every result', async () => {
    state.busy.add('s1');
    requestWake('s1', ['t1']);
    requestWake('s1', ['t2', 't1']);
    await settle();
    expect(sendChatTurn).not.toHaveBeenCalled();
    state.busy.delete('s1');
    flushPendingWakes();
    flushPendingWakes();
    await settle();
    expect(sendChatTurn.mock.calls).toEqual([wakeOf('s1', ['t1', 't2'])]);
  });

  it('waits for a free slot under the active-session ceiling', async () => {
    state.active = 3;
    requestWake('s1', ['t1']);
    await settle();
    expect(sendChatTurn).not.toHaveBeenCalled();
    state.active = 2;
    flushPendingWakes();
    await settle();
    expect(sendChatTurn).toHaveBeenCalledTimes(1);
  });

  it('never wakes a session this window has not loaded', async () => {
    requestWake('elsewhere', ['t1']);
    flushPendingWakes();
    await settle();
    expect(sendChatTurn).not.toHaveBeenCalled();
  });

  it('drops a waiting wake whose session was evicted meanwhile', async () => {
    state.busy.add('s1');
    requestWake('s1', ['t1']);
    state.hydrated.clear();
    state.busy.delete('s1');
    flushPendingWakes();
    state.hydrated.add('s1');
    flushPendingWakes();
    await settle();
    expect(sendChatTurn).not.toHaveBeenCalled();
  });
});
