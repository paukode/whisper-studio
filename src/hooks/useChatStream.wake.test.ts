/**
 * A wake turn (./chatStream/backgroundWake) is an ordinary chat turn the app
 * starts for a session when background commands it started have finished:
 * it targets that session (not the one on screen), adds no user message, asks
 * the server to word the request (`runtime_wake`), and ships the result row
 * that woke it as history.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sendChatTurn } from './useChatStream';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';

function emptySSEResponse(): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const REPORT = {
  task_id: 't1',
  command: 'make release',
  status: 'completed',
  exit_code: 0,
  output_tail: 'built dist/App.dmg',
  timestamp: '2026-10-28T17:03:22Z',
};

describe('sendChatTurn for a wake', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: 'on-screen', liveSessions: {}, sessions: [] });
  });
  afterEach(() => vi.restoreAllMocks());

  it('runs in its own session, adds no message and ships the result that woke it', async () => {
    const bodies: Record<string, unknown>[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      bodies.push(JSON.parse(String(init?.body ?? '{}')));
      return emptySSEResponse();
    });
    const chat = getChatStore('woken');
    chat.getState().addMessage({ role: 'user', content: 'build it', timestamp: 't0' });
    chat.getState().addMessage({ role: 'assistant', content: 'It runs in the background.', timestamp: 't1' });
    chat.getState().addMessage({ role: 'task_report', content: '', timestamp: 't2', taskReport: REPORT });

    await sendChatTurn('', { sessionId: 'woken', hideUserMessage: true, runtimeWake: { task_ids: ['t1'] } });

    const [body] = bodies.filter((b) => 'runtime_wake' in b);
    expect(body.session_id).toBe('woken');
    expect(body.runtime_wake).toEqual({ task_ids: ['t1'] });
    const history = body.history as Array<Record<string, unknown>>;
    expect(history[history.length - 1]).toMatchObject({ role: 'task_report', taskReport: REPORT });
    expect(chat.getState().messages.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(getChatStore('on-screen').getState().messages).toEqual([]);
  });

  it('stays quiet when the server finds a turn already running', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ queued_into_running_turn: false, error: 'session_busy' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');
    await sendChatTurn('', { sessionId: 'woken-busy', hideUserMessage: true, runtimeWake: { task_ids: ['t1'] } });
    expect(addToast).not.toHaveBeenCalled();
    expect(getChatStore('woken-busy').getState().isStreaming).toBe(false);
  });
});
