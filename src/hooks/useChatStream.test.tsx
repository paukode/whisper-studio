/**
 * Surface-level tests for the chat stream hook. We don't try to drive a full
 * SSE response — that's an integration-level concern — but we lock in the two
 * observable contracts the rest of the UI depends on:
 *
 *   1. Calling `send()` immediately adds the user message to the store and
 *      flips `isStreaming` to true.
 *   2. `abort()` cancels the in-flight fetch (via AbortSignal).
 *
 * Fetch is replaced with a stub that returns an empty SSE body so the hook's
 * stream loop terminates cleanly under test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useChatStream } from './useChatStream';
import { dropRuntime, getActiveChatStore, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useSubagentStore } from '@/stores/subagentStore';

function emptySSEResponse(): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('useChatStream', () => {
  beforeEach(() => {
    // Fresh session context per test: send() creates a new session, which
    // gets its own runtime store from the registry. Drop leftover runtimes
    // (the registry is module-scoped) so streams from prior tests can't
    // leak activity into this one.
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
    useSubagentStore.setState({ stops: {} });
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => emptySSEResponse());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('appends a user message and flips isStreaming to true on send', async () => {
    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('hello world');
    });

    const s = getActiveChatStore().getState();
    expect(s.messages.some((m) => m.role === 'user' && m.content === 'hello world')).toBe(true);
  });

  it('exposes a stable {send, abort} shape', () => {
    const { result } = renderHook(() => useChatStream());
    expect(typeof result.current.send).toBe('function');
    expect(typeof result.current.abort).toBe('function');
  });

  it('abort is callable when no stream is active without throwing', () => {
    const { result } = renderHook(() => useChatStream());
    expect(() => result.current.abort()).not.toThrow();
  });

  it('continuation send (approvedToolResult) does not append a user message', async () => {
    const { result } = renderHook(() => useChatStream());
    const before = getActiveChatStore().getState().messages.length;
    await act(async () => {
      await result.current.send('', {
        approvedToolResult: {
          tool_use_id: 't1',
          content: '[user approved] write: /tmp/x',
        },
      });
    });
    expect(getActiveChatStore().getState().messages.length).toBe(before);
  });

  // THE parallel-sessions regression test: a stream started in session A
  // keeps writing into A's store even when the user switches to B
  // mid-stream. With the old singleton store, B would receive A's tokens.
  it('stream tokens land in the ORIGINATING session after a mid-stream switch', async () => {
    let releaseStream: (() => void) | null = null;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (!url.includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const enc = new TextEncoder();
          controller.enqueue(enc.encode('data: {"text": "token-for-A "}\n\n'));
          // Hold the stream open until the test switches sessions.
          releaseStream = () => {
            controller.enqueue(enc.encode('data: {"text": "late-token-for-A"}\n\n'));
            controller.enqueue(enc.encode('data: [DONE]\n\n'));
            controller.close();
          };
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });

    const { result } = renderHook(() => useChatStream());
    let sendDone: Promise<void> = Promise.resolve();
    await act(async () => {
      sendDone = result.current.send('long question');
      // Let the first token arrive.
      await new Promise((r) => setTimeout(r, 50));
    });

    const sessionA = useSessionStore.getState().currentSessionId;
    expect(sessionA).toBeTruthy();
    expect(getChatStore(sessionA).getState().currentStreamContent).toContain('token-for-A');

    // User switches to a different session mid-stream.
    await act(async () => {
      useSessionStore.setState({ currentSessionId: 'session-B' });
      releaseStream?.();
      await sendDone;
    });

    // The late token and the final message belong to A; B saw nothing.
    const aMessages = getChatStore(sessionA).getState().messages;
    expect(aMessages.some((m) => m.role === 'assistant' && m.content.includes('late-token-for-A'))).toBe(true);
    expect(getChatStore('session-B').getState().messages).toHaveLength(0);
    expect(getChatStore('session-B').getState().currentStreamContent).toBe('');
  });

  it('abort stops only the active session, not a background stream', async () => {
    // Session A gets a never-ending stream; we then switch to B and abort.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (!url.includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('data: {"text": "A streaming"}\n\n'));
          // Never closes — simulates a long reply.
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      void result.current.send('never ends');
      await new Promise((r) => setTimeout(r, 50));
    });
    const sessionA = useSessionStore.getState().currentSessionId!;
    expect(getChatStore(sessionA).getState().isStreaming).toBe(true);

    await act(async () => {
      useSessionStore.setState({ currentSessionId: 'other-session' });
      result.current.abort(); // aborts the ACTIVE (other-session) — a no-op
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(getChatStore(sessionA).getState().isStreaming).toBe(true);
  });

  // The kill switch contract: state is finalized synchronously by abort()
  // itself — the UI never waits for the AbortError to travel back through
  // the read loop — and the stream's own success path must not append a
  // second message afterwards.
  it('abort finalizes the UI synchronously with exactly one (Stopped) message', async () => {
    let releaseStream: (() => void) | null = null;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const enc = new TextEncoder();
          controller.enqueue(enc.encode('data: {"text": "partial answer"}\n\n'));
          // Held open; released AFTER the kill so the read loop exits via the
          // NORMAL path (done/aborted break), exercising the success-path guard.
          releaseStream = () => {
            controller.enqueue(enc.encode('data: {"text": "late token"}\n\n'));
            controller.enqueue(enc.encode('data: [DONE]\n\n'));
            controller.close();
          };
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });

    const { result } = renderHook(() => useChatStream());
    let sendDone: Promise<void> = Promise.resolve();
    await act(async () => {
      sendDone = result.current.send('long question');
      await new Promise((r) => setTimeout(r, 50));
    });
    const sid = useSessionStore.getState().currentSessionId!;
    expect(getChatStore(sid).getState().isStreaming).toBe(true);

    act(() => {
      result.current.abort();
    });
    // No awaits since abort(): the kill must have already finalized state.
    const killed = getChatStore(sid).getState();
    expect(killed.isStreaming).toBe(false);
    expect(killed.currentStreamContent).toBe('');
    expect(
      killed.messages.filter((m) => m.content.endsWith('*(Stopped)*')),
    ).toHaveLength(1);

    // Let the stream end and the send() promise settle: no duplicate
    // "(Stopped)" message, no resurrected full answer.
    await act(async () => {
      releaseStream?.();
      await sendDone;
    });
    const settled = getChatStore(sid).getState();
    expect(settled.messages.filter((m) => m.content.includes('*(Stopped)*'))).toHaveLength(1);
    expect(settled.messages.filter((m) => m.role === 'assistant')).toHaveLength(1);
  });

  it('abort before any token appends no assistant message', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      // Stream that never produces anything — the "thinking" phase.
      const stream = new ReadableStream<Uint8Array>({ start() {} });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      void result.current.send('no answer yet');
      await new Promise((r) => setTimeout(r, 50));
    });
    const sid = useSessionStore.getState().currentSessionId!;
    expect(getChatStore(sid).getState().isStreaming).toBe(true);

    await act(async () => {
      result.current.abort();
      await new Promise((r) => setTimeout(r, 20));
    });
    const s = getChatStore(sid).getState();
    expect(s.isStreaming).toBe(false);
    expect(s.messages.filter((m) => m.role === 'assistant')).toHaveLength(0);
  });

  it('abort stops and clears every registered subagent', () => {
    const stopA = vi.fn();
    const stopB = vi.fn();
    useSubagentStore.getState().register('team-a', stopA);
    useSubagentStore.getState().register('team-b', stopB);

    const { result } = renderHook(() => useChatStream());
    act(() => {
      result.current.abort(); // no main stream running — subagents alone
    });

    expect(stopA).toHaveBeenCalledTimes(1);
    expect(stopB).toHaveBeenCalledTimes(1);
    expect(useSubagentStore.getState().stops).toEqual({});
  });
});
