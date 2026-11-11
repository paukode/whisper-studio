import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { MAX_ACTIVE_SESSIONS, sendChatTurn } from './useChatStream';

function sseDone(): Response {
  return new Response(new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(new TextEncoder().encode('data: [DONE]\n\n')); c.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

/** Sessions with a reply in flight, as the stream leaves them. */
function busySessions(count: number): void {
  for (let i = 0; i < count; i++) getChatStore(`busy-${i}`).getState().startTurn('streaming');
}

const chatPosts = () =>
  vi.mocked(globalThis.fetch).mock.calls.filter((c) => String(c[0]).includes('/api/chat'));
const limitToast = () => useUIStore.getState().toasts.find((t) => t.key === 'parallel-cap');

beforeEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: 'next', liveSessions: {}, sessions: [] });
  useUIStore.setState({ toasts: [] });
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => sseDone());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the parallel session limit', () => {
  it('starts a session while the others fill all but one slot', async () => {
    busySessions(MAX_ACTIVE_SESSIONS - 1);

    expect(await sendChatTurn('Hello.', { sessionId: 'next' })).toBe(true);

    expect(chatPosts()).toHaveLength(1);
    expect(limitToast()).toBeUndefined();
  });

  it('refuses one more session with the toast, and sends nothing', async () => {
    busySessions(MAX_ACTIVE_SESSIONS);

    expect(await sendChatTurn('Hello.', { sessionId: 'next' })).toBe(false);

    expect(chatPosts()).toHaveLength(0);
    expect(limitToast()?.message).toBe(
      `${MAX_ACTIVE_SESSIONS} sessions are already active. Wait for one to finish (or stop it) before starting another.`,
    );
  });
});
