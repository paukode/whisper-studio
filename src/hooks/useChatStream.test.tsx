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
import { killSessionStream, useChatStream } from './useChatStream';
import { dropRuntime, getActiveChatStore, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useSubagentStore } from '@/stores/subagentStore';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { registerStreamController } from './chatStream/streamControl';

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
    useSubagentStore.setState({ stops: {}, owners: {} });
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

  it("refreshes the session readout after a turn, so a GPT turn's cost carries its note", async () => {
    const NOTE = 'Estimate at list rates. AWS billed GPT on Bedrock at $0 for this account as of 2026-09-23.';
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (String(input).includes('/api/costs/session/')) {
        return new Response(
          JSON.stringify({ prompt_tokens: 900, output_tokens: 30, cost_usd: 0.2, note: NOTE, estimated_rounds: 0 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return emptySSEResponse();
    });
    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('hello');
    });
    await vi.waitFor(() => expect(getActiveChatStore().getState().sessionCostNote).toBe(NOTE));
  });

  it('exposes a stable {send, sendMidTurn, abort} shape', () => {
    const { result } = renderHook(() => useChatStream());
    expect(typeof result.current.send).toBe('function');
    expect(typeof result.current.sendMidTurn).toBe('function');
    expect(typeof result.current.abort).toBe('function');
  });

  it('abort is callable when no stream is active without throwing', () => {
    const { result } = renderHook(() => useChatStream());
    expect(() => result.current.abort()).not.toThrow();
  });

  describe('sendMidTurn', () => {
    it('confirmed delivery: shows the message and resolves true, without touching isStreaming', async () => {
      useSessionStore.setState({ currentSessionId: 'mid-turn-sess', liveSessions: {}, sessions: [] });
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(
        new Response(JSON.stringify({ queued_into_running_turn: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
      const store = getChatStore('mid-turn-sess');
      store.getState().setStreaming(true); // a turn is presumed already running

      const { result } = renderHook(() => useChatStream());
      let delivered: boolean | undefined;
      await act(async () => {
        delivered = await result.current.sendMidTurn('stop and give me what you have');
      });

      expect(delivered).toBe(true);
      const s = store.getState();
      expect(
        s.messages.some(
          (m) => m.role === 'user' && m.content === 'stop and give me what you have',
        ),
      ).toBe(true);
      // Untouched — the ALREADY-running turn owns isStreaming, not this call.
      expect(s.isStreaming).toBe(true);
    });

    it('not delivered: shows nothing, resolves false, and closes the stray response', async () => {
      useSessionStore.setState({ currentSessionId: 'race-sess', liveSessions: {}, sessions: [] });
      const cancel = vi.fn();
      vi.spyOn(globalThis, 'fetch').mockResolvedValue({
        ok: true,
        headers: { get: () => 'text/event-stream' },
        body: { cancel },
      } as unknown as Response);

      const { result } = renderHook(() => useChatStream());
      let delivered: boolean | undefined;
      await act(async () => {
        delivered = await result.current.sendMidTurn('are you still there');
      });

      expect(delivered).toBe(false);
      expect(cancel).toHaveBeenCalled();
      // Strictly binary — nothing is shown for an attempt that wasn't delivered.
      const s = getChatStore('race-sess').getState();
      expect(s.messages.some((m) => m.role === 'user' && m.content === 'are you still there')).toBe(false);
    });

    it('a JSON 200 that does not confirm the queue is not delivered', async () => {
      useSessionStore.setState({ currentSessionId: 'unconfirmed-sess', liveSessions: {}, sessions: [] });
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(
        new Response(JSON.stringify({ queued_into_running_turn: false }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

      const { result } = renderHook(() => useChatStream());
      let delivered: boolean | undefined;
      await act(async () => {
        delivered = await result.current.sendMidTurn('still there?');
      });

      expect(delivered).toBe(false);
      const s = getChatStore('unconfirmed-sess').getState();
      expect(s.messages.some((m) => m.role === 'user' && m.content === 'still there?')).toBe(false);
    });

    it('a slow confirmation never queues the same message twice', async () => {
      // Reported as "not sent, then it showed up five times": the server
      // answered late, every extra Enter posted the text again, and each copy
      // was queued. Presses while the first delivery is open send nothing.
      useSessionStore.setState({ currentSessionId: 'slow-sess', liveSessions: {}, sessions: [] });
      useUIStore.setState({ toasts: [] });
      let answer: (r: Response) => void = () => {};
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
        (input) => String(input).includes('/api/chat')
          ? new Promise<Response>((resolve) => { answer = resolve; })
          : Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })),
      );
      const store = getChatStore('slow-sess');
      store.getState().setStreaming(true);
      const chatCalls = () => fetchSpy.mock.calls.filter((c) => String(c[0]).includes('/api/chat'));

      const { result } = renderHook(() => useChatStream());
      let first: Promise<boolean> = Promise.resolve(false);
      const retries: boolean[] = [];
      await act(async () => {
        first = result.current.sendMidTurn('did you push the branch?');
        for (let i = 0; i < 4; i++) retries.push(await result.current.sendMidTurn('did you push the branch?'));
      });
      expect(chatCalls()).toHaveLength(1);
      expect(retries).toEqual([false, false, false, false]);
      expect(useUIStore.getState().toasts.some((t) => /still delivering/i.test(t.message))).toBe(true);

      let delivered: boolean | undefined;
      await act(async () => {
        answer(new Response(JSON.stringify({ queued_into_running_turn: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }));
        delivered = await first;
      });
      expect(delivered).toBe(true);
      const bubbles = store.getState().messages.filter(
        (m) => m.role === 'user' && m.content === 'did you push the branch?',
      );
      expect(bubbles).toHaveLength(1);

      // The next message goes out once the first one has landed.
      await act(async () => {
        void result.current.sendMidTurn('and open the MR');
      });
      expect(chatCalls()).toHaveLength(2);
    });

    it('sends nothing when there is no active session, and says so', async () => {
      // The one outcome that used to be silent. From the composer a silent
      // false is indistinguishable from a dead Enter key, which is how this
      // reached us as "I cannot send and the button seems locked".
      useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
      useUIStore.setState({ toasts: [] });
      const fetchSpy = vi.spyOn(globalThis, 'fetch');
      const { result } = renderHook(() => useChatStream());
      let delivered: boolean | undefined;
      await act(async () => {
        delivered = await result.current.sendMidTurn('hello?');
      });
      expect(delivered).toBe(false);
      // The toast records itself server-side, so assert on the turn instead
      // of on fetch as a whole: nothing was sent to the chat endpoint.
      expect(
        fetchSpy.mock.calls.filter((c) => String(c[0]).includes('/api/chat')),
      ).toHaveLength(0);
      const shown = useUIStore.getState().toasts;
      expect(shown).toHaveLength(1);
      expect(shown[0].type).toBe('error');
      expect(shown[0].message).toMatch(/not delivered/i);
    });
  });

  // A JSON body is never an SSE stream. Fed to the SSE reader it parsed zero
  // events, so the send ended with the user's bubble and nothing else.
  describe('send() JSON replies', () => {
    function chatJson(status: number, body: unknown): void {
      vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
        if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
        return new Response(JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    }

    it('a reply queued into the running turn keeps the bubble and says where it went', async () => {
      useSessionStore.setState({ currentSessionId: 'queued-sess', liveSessions: {}, sessions: [] });
      useUIStore.setState({ toasts: [] });
      chatJson(200, { queued_into_running_turn: true });

      const { result } = renderHook(() => useChatStream());
      await act(async () => {
        await result.current.send('are you there?');
      });

      const s = getChatStore('queued-sess').getState();
      expect(s.messages.some((m) => m.role === 'user' && m.content === 'are you there?')).toBe(true);
      expect(s.isStreaming).toBe(false);
      // The same notice the composer's steer path shows for a delivered text.
      const queuedToast = useUIStore.getState().toasts.find((t) => t.key === 'midturn-queued');
      expect(queuedToast?.type).toBe('info');
    });

    // The race the plain queued case cannot show: this send aborts the
    // session's own reply first, and the server, which frees that turn's slot
    // only once it notices the disconnect, queues the text into it.
    it('a reply queued into the turn this send just replaced is not reported as delivered', async () => {
      useSessionStore.setState({ currentSessionId: 'replace-sess', liveSessions: {}, sessions: [] });
      useUIStore.setState({ toasts: [] });
      const liveReply = new AbortController();
      registerStreamController('replace-sess', liveReply);
      getChatStore('replace-sess').getState().setStreaming(true);
      chatJson(200, { queued_into_running_turn: true });

      const { result } = renderHook(() => useChatStream());
      await act(async () => {
        await result.current.send('summarize this line');
      });

      expect(liveReply.signal.aborted).toBe(true);
      const s = getChatStore('replace-sess').getState();
      // The text stays on screen so it can be sent again.
      expect(s.messages.some((m) => m.role === 'user' && m.content === 'summarize this line')).toBe(true);
      expect(s.isStreaming).toBe(false);
      const toasts = useUIStore.getState().toasts;
      expect(toasts.some((t) => t.key === 'midturn-queued')).toBe(false);
      const notAnswered = toasts.find((t) => t.key === 'queued-into-replaced-turn');
      expect(notAnswered?.type).toBe('error');
      expect(notAnswered?.message).toMatch(/not answered/i);
    });

    it('a JSON refusal shows the server reason instead of going silent', async () => {
      useSessionStore.setState({ currentSessionId: 'refused-sess', liveSessions: {}, sessions: [] });
      chatJson(400, { error: 'This model is not available in Local mode.' });

      const { result } = renderHook(() => useChatStream());
      await act(async () => {
        await result.current.send('hello');
      });

      const s = getChatStore('refused-sess').getState();
      const last = s.messages[s.messages.length - 1];
      expect(last.role).toBe('assistant');
      expect(last.content).toContain('This model is not available in Local mode.');
      expect(s.isStreaming).toBe(false);
    });

    it('a 200 JSON body that is not a queue confirmation is reported, not swallowed', async () => {
      useSessionStore.setState({ currentSessionId: 'odd-sess', liveSessions: {}, sessions: [] });
      useUIStore.setState({ toasts: [] });
      chatJson(200, { queued_into_running_turn: false });

      const { result } = renderHook(() => useChatStream());
      await act(async () => {
        await result.current.send('hello');
      });

      const s = getChatStore('odd-sess').getState();
      expect(s.messages[s.messages.length - 1].role).toBe('assistant');
      expect(s.messages[s.messages.length - 1].content).toMatch(/Error/);
      expect(useUIStore.getState().toasts.some((t) => t.key === 'midturn-queued')).toBe(false);
    });
  });

  /** A /api/chat response built from raw SSE frames; anything else 200s empty. */
  function chatStreamOf(frames: unknown[]): void {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const enc = new TextEncoder();
          for (const f of frames) controller.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
          controller.enqueue(enc.encode('data: [DONE]\n\n'));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });
  }

  // The whole point of the mid-turn flush: an approval card is appended the
  // moment its frame arrives, so unless the text before it is committed first
  // the card is hoisted above the entire turn and its Approve button scrolls
  // out of reach.
  it('renders an approval card between the text before it and the text after it', async () => {
    chatStreamOf([
      { text: "I'll set up a repair workflow." },
      { workflow_preview: { script: 'export const meta = {}', name: 'repair-v2' } },
      { text: 'Approve the card when you are ready.' },
    ]);

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('fix the regressions');
    });

    const messages = getActiveChatStore().getState().messages;
    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'assistant', 'assistant']);
    expect(messages[1].content).toBe("I'll set up a repair workflow.");
    expect(messages[2].toolUse?.[0].toolName).toBe('workflow_preview');
    expect(messages[3].content).toBe('Approve the card when you are ready.');
  });

  it('keeps the tools a turn ran after a card, and calls no such turn silent', async () => {
    // No closing text: the turn's words were already committed above the card,
    // so there is nothing left for a final message — but the work done after it
    // still has to reach the transcript, and the empty-answer fallback must not
    // announce "no text response" under a reply the model plainly gave.
    chatStreamOf([
      { text: 'Proposing the workflow now.' },
      { workflow_preview: { script: 'export const meta = {}', name: 'repair-v2' } },
      { skill: 'cron_create' },
      { skill_result: 'cron_create', output: 'Scheduled every 10 min' },
    ]);

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('fix the regressions');
    });

    const messages = getActiveChatStore().getState().messages;
    expect(messages[1].content).toBe('Proposing the workflow now.');
    expect(messages[2].toolUse?.[0].toolName).toBe('workflow_preview');
    expect(messages[3].toolUse?.[0].toolName).toBe('cron_create');
    expect(messages.some((m) => /No text response|without text/.test(m.content))).toBe(false);
  });

  it('an approval card remembers the model and effort its turn was sent with', async () => {
    useSettingsStore.setState({ selectedModel: 'cloud-opus', effortLevel: 'high' });
    chatStreamOf([
      {
        approval_request: {
          tool_use_id: 'tu_run',
          action: 'terminal_run',
          category: 'cli',
          preview: 'command',
          summary: 'npm run build',
          payload: { command: 'npm run build' },
        },
      },
    ]);

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('build it');
    });
    const sent = (globalThis.fetch as unknown as { mock: { calls: Array<[unknown, RequestInit]> } })
      .mock.calls.find((c) => String(c[0]).includes('/api/chat'))!;
    const body = JSON.parse(String(sent[1].body)) as { model: string; effort_level: string };

    // Another session picks a different model while the card waits.
    useSettingsStore.setState({ selectedModel: 'local_gemma', effortLevel: 'low' });

    const card = getActiveChatStore().getState().currentApproval;
    expect(card?.turnSettings.model).toBe(body.model);
    expect(card?.turnSettings.effort_level).toBe(body.effort_level);
  });

  // Question and folder-prompt cards pause the turn the same way an approval
  // does, and their answer is the resumed half of it: it must not pick up a
  // model chosen elsewhere while the card waited.
  describe('an answered card resumes on its own turn\'s settings', () => {
    function bodies(): Array<{ model: string; effort_level: string }> {
      const mock = globalThis.fetch as unknown as { mock: { calls: Array<[unknown, RequestInit]> } };
      return mock.mock.calls
        .filter((c) => String(c[0]).includes('/api/chat'))
        .map((c) => JSON.parse(String(c[1].body)) as { model: string; effort_level: string });
    }

    it.each([
      ['a question', { user_question: { question: 'Which DB?', options: ['sqlite', 'pg'], tool_use_id: 'tu_card' } }],
      ['a folder prompt', { ws_workspace_prompt: { reason: 'no_workspace', suggested: '', recent: [], tool_use_id: 'tu_card' } }],
    ])('%s', async (_label, frame) => {
      useSessionStore.setState({ currentSessionId: 'card-sess', liveSessions: {}, sessions: [] });
      useSettingsStore.setState({ selectedModel: 'cloud-opus', effortLevel: 'high' });
      chatStreamOf([frame]);
      const { result } = renderHook(() => useChatStream());
      await act(async () => {
        await result.current.send('set it up');
      });

      // Another session picks an on-device model while the card waits.
      useSettingsStore.setState({ selectedModel: 'local_gemma', effortLevel: 'low' });
      await act(async () => {
        await result.current.send('', { approvedToolResult: [{ tool_use_id: 'tu_card', content: 'sqlite' }] });
      });

      const [first, resumed] = bodies();
      expect(resumed.model).toBe(first.model);
      expect(resumed.effort_level).toBe(first.effort_level);
    });
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

  it('abort stops only the subagents the viewed session started', () => {
    const stopA = vi.fn();
    const stopB = vi.fn();
    useSubagentStore.getState().register('team-a', 'sess-A', stopA);
    useSubagentStore.getState().register('team-b', 'sess-B', stopB);

    const { result } = renderHook(() => useChatStream());
    act(() => {
      useSessionStore.setState({ currentSessionId: 'sess-B' });
      result.current.abort(); // no main stream running: subagents alone
    });

    // Session B's run is stopped and gone; session A's keeps working.
    expect(stopB).toHaveBeenCalledTimes(1);
    expect(useSubagentStore.getState().stops['team-b']).toBeUndefined();
    expect(stopA).not.toHaveBeenCalled();
    expect(useSubagentStore.getState().stops['team-a']).toBe(stopA);

    act(() => {
      useSessionStore.setState({ currentSessionId: 'sess-A' });
      result.current.abort();
    });
    expect(stopA).toHaveBeenCalledTimes(1);
    expect(useSubagentStore.getState().stops['team-a']).toBeUndefined();
  });

  // Stop scopes its kill to the start of the running TURN. An answered
  // question card resumes the turn that asked, so work that turn began before
  // the card (a background dev server it launched) stays in reach; only a new
  // message starts a new turn.
  it('an answered card keeps the turn start that Stop sends; a new message resets it', async () => {
    const stopSince: number[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/shell/tasks/stop')) {
        stopSince.push(JSON.parse(String(init?.body)).since);
        return new Response('{}', { status: 200 });
      }
      if (!url.includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('data: {"text": "working"}\n\n'));
          // Never closes: each Stop lands mid-stream.
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });
    const now = vi.spyOn(Date, 'now');
    const { result } = renderHook(() => useChatStream());
    const sendAt = async (at: number, ...args: Parameters<typeof result.current.send>) => {
      now.mockReturnValue(at);
      await act(async () => {
        void result.current.send(...args);
        await new Promise((r) => setTimeout(r, 30));
      });
    };

    await sendAt(7_000_000, 'set up the app and ask me which port');
    const sid = useSessionStore.getState().currentSessionId!;
    await sendAt(7_120_000, '', { approvedToolResult: { tool_use_id: 'q1', content: '5173' } });
    act(() => killSessionStream(sid));
    expect(stopSince[stopSince.length - 1]).toBe(7_000);

    await sendAt(7_200_000, 'now something else');
    act(() => killSessionStream(sid));
    expect(stopSince[stopSince.length - 1]).toBe(7_200);
  });
});
