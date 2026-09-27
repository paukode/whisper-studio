/**
 * The window between "Yes" on an approval card and the resumed turn's stream.
 *
 * The paused turn had already ended, so while the approved action ran (up to
 * minutes for a shell command) nothing said the session was working: no Stop,
 * no ESC, a typed "continue" started a second turn that the continuation then
 * took over, a Stop was ignored because the turn resumed when the action
 * returned, and the runtime registry could evict the session so the reply
 * landed in a store nobody saved. These tests pin how the leg relates to each
 * of those readers. Only the action executor and the network are faked.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalOutcome } from '@/api/approval';
import type { PendingApproval } from '@/stores/chatStore';
import {
  countActiveSessions,
  dropRuntime,
  getChatStore,
  getRuntime,
  maybeEvictIdle,
  useRuntimeIndex,
} from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { useChatStream } from '@/hooks/useChatStream';
import { APPROVED_ACTION_STATUS, answerApproval } from './approvalLeg';
import { hasLiveStream, killSessionStream, registerStreamController } from './streamControl';

let finishAction: ((outcome: ApprovalOutcome) => void) | null = null;
const executeApproval = vi.fn();
vi.mock('@/api/approval', () => ({
  executeApproval: (...args: unknown[]) => executeApproval(...args),
}));

const SID = 'sess-leg';

function approval(): PendingApproval {
  return {
    toolUseId: 'tu_dev',
    action: 'terminal_run',
    category: 'cli',
    preview: 'command',
    summary: 'npm run build',
    payload: { command: 'npm run build' },
    riskHint: null,
    explanation: null,
    sessionId: SID,
    alwaysAsks: false,
    turnSettings: { model: 'm', effort_level: 'normal', verbosity: 'medium', brief_mode: false },
  };
}

/** /api/chat requests the code under test made (continuations included). */
function chatCalls(): Array<Record<string, unknown>> {
  const mock = globalThis.fetch as unknown as { mock: { calls: Array<[unknown, RequestInit?]> } };
  return mock.mock.calls
    .filter((c) => String(c[0]).includes('/api/chat'))
    .map((c) => JSON.parse(String(c[1]?.body ?? '{}')) as Record<string, unknown>);
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe('approval leg', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [], saveSession: vi.fn() } as never);
    useUIStore.setState({ toasts: [] });
    executeApproval.mockReset().mockImplementation(
      () => new Promise<ApprovalOutcome>((resolve) => { finishAction = resolve; }),
    );
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          const enc = new TextEncoder();
          c.enqueue(enc.encode('data: {"text": "build finished"}\n\n'));
          c.enqueue(enc.encode('data: [DONE]\n\n'));
          c.close();
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });
    getChatStore(SID).getState().enqueueApproval(approval());
  });

  afterEach(() => {
    finishAction = null;
    vi.restoreAllMocks();
  });

  it('keeps the session busy from the click until the resumed turn has the stream', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });

    // The card is gone, and every reader of "is this session working" says yes.
    const s = getChatStore(SID).getState();
    expect(s.currentApproval).toBeNull();
    expect(s.isStreaming).toBe(true);
    expect(s.streamStatus).toBe(APPROVED_ACTION_STATUS);
    expect(hasLiveStream(SID)).toBe(true);
    expect(countActiveSessions()).toBe(1);
    expect(chatCalls()).toHaveLength(0);

    await act(async () => {
      finishAction!({ ok: true, output: 'built' });
      await answered;
    });
    expect(chatCalls()).toHaveLength(1);
    const after = getChatStore(SID).getState();
    expect(after.isStreaming).toBe(false);
    expect(after.messages.some((m) => m.content === 'build finished')).toBe(true);
    expect(hasLiveStream(SID)).toBe(false);
  });

  it('is never evicted while the approved action runs', async () => {
    await act(async () => {
      void answerApproval(approval(), true, false);
      await tick();
    });
    // The user moves on: three other sessions become live and SID is the
    // least recently used one.
    useSessionStore.setState({ currentSessionId: 'other-3' });
    getRuntime(SID).hydrated = true;
    getRuntime(SID).lastUsed = 0;
    ['other-1', 'other-2', 'other-3'].forEach((id, i) => {
      const e = getRuntime(id);
      e.hydrated = true;
      e.lastUsed = i + 1;
    });

    maybeEvictIdle();
    expect(useRuntimeIndex.getState().liveIds).toContain(SID);
    await act(async () => { finishAction!({ ok: true }); await tick(); await tick(); });
  });

  it('Stop during the action ends the turn there and records what the action did', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });

    act(() => killSessionStream(SID));
    // Instant: the session is idle the moment Stop returns.
    expect(getChatStore(SID).getState().isStreaming).toBe(false);

    await act(async () => {
      finishAction!({ ok: true, output: 'built' });
      await answered;
    });
    // The turn does not resume behind the user's back...
    expect(chatCalls()).toHaveLength(0);
    expect(getChatStore(SID).getState().isStreaming).toBe(false);
    // ...and the next turn's history says the action did run.
    const msgs = getChatStore(SID).getState().messages;
    const last = msgs[msgs.length - 1];
    expect(last?.stopped).toBe(true);
    expect(last?.content).toContain('terminal_run');
    expect(last?.content).toContain('completed');
  });

  it('says Stop ended an action the server killed, and never promised it would finish', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    act(() => killSessionStream(SID));
    const atStop = getChatStore(SID).getState().messages;
    const interim = atStop[atStop.length - 1]?.content ?? '';
    expect(interim).toContain('Its result will be recorded here.');
    expect(interim).not.toContain('finishing on its own');

    await act(async () => {
      finishAction!({ ok: false, stopped: true, error: 'stopped before it finished' });
      await answered;
    });
    const done = getChatStore(SID).getState().messages;
    const row = done[done.length - 1]?.content ?? '';
    expect(row).toContain('Stop ended it before it finished.');
    expect(row).not.toContain('It failed');
  });

  // The row has to be written when the stop happens: written when the action
  // returned, it landed after whatever the user sent in between, and the
  // turn answering that question never learned the action had run.
  it('Stop writes the action row at once, so a question sent next reads it', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    act(() => killSessionStream(SID));
    const atStop = getChatStore(SID).getState().messages;
    const rowAtStop = atStop[atStop.length - 1];
    expect(rowAtStop?.stopped).toBe(true);
    expect(rowAtStop?.content).toContain('terminal_run');

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('what happened?');
    });
    const history = chatCalls()[0].history as Array<{ content: string }>;
    expect(history.some((h) => h.content === rowAtStop?.content)).toBe(true);

    await act(async () => {
      finishAction!({ ok: true, output: 'built' });
      await answered;
    });
    // Filled in where it stands: still before the question, now with the result.
    const msgs = getChatStore(SID).getState().messages;
    const rowIdx = msgs.findIndex((m) => m.content.includes('terminal_run'));
    const askIdx = msgs.findIndex((m) => m.role === 'user' && m.content === 'what happened?');
    expect(rowIdx).toBeGreaterThanOrEqual(0);
    expect(rowIdx).toBeLessThan(askIdx);
    expect(msgs[rowIdx].content).toContain('It completed.');
    expect(chatCalls()).toHaveLength(1); // the stopped turn never resumed
  });

  it('a new send during the action records it before the new question, not as a Stop', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });

    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('summarize the log');
    });
    const msgs = getChatStore(SID).getState().messages;
    const row = msgs.find((m) => m.content.includes('terminal_run'));
    expect(row?.stopped).toBeUndefined();
    expect(row?.content).toMatch(/replaced the turn/);
    expect(msgs.indexOf(row!)).toBeLessThan(
      msgs.findIndex((m) => m.role === 'user' && m.content === 'summarize the log'),
    );
    const history = chatCalls()[0].history as Array<{ content: string }>;
    expect(history.some((h) => h.content === row?.content)).toBe(true);

    await act(async () => {
      finishAction!({ ok: false, error: 'exit 1' });
      await answered;
    });
    const filled = getChatStore(SID).getState().messages.find((m) => m.content.includes('terminal_run'));
    expect(filled?.content).toContain('It failed: exit 1.');
    expect(chatCalls()).toHaveLength(1);
  });

  it("keeps a failed action's output next to its exit code", async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    const { result } = renderHook(() => useChatStream());
    await act(async () => {
      await result.current.send('summarize the log');
    });

    await act(async () => {
      finishAction!({ ok: false, error: 'exit code 1', output: 'npm ERR! Missing script: "build"' });
      await answered;
    });
    const row = getChatStore(SID).getState().messages.find((m) => m.content.includes('terminal_run'));
    expect(row?.content).toContain('It failed: exit code 1');
    expect(row?.content).toContain('Missing script: "build"');
  });

  it('after Stop the session stays live until the action outcome is saved', async () => {
    const debouncedSave = vi.fn();
    useSessionStore.setState({ debouncedSave } as never);
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    act(() => killSessionStream(SID));
    debouncedSave.mockClear();

    useSessionStore.setState({ currentSessionId: 'other-3' });
    getRuntime(SID).hydrated = true;
    getRuntime(SID).lastUsed = 0;
    ['other-1', 'other-2', 'other-3'].forEach((id, i) => {
      const e = getRuntime(id);
      e.hydrated = true;
      e.lastUsed = i + 1;
    });
    maybeEvictIdle();
    expect(useRuntimeIndex.getState().liveIds).toContain(SID);

    await act(async () => {
      finishAction!({ ok: true });
      await answered;
    });
    const done = getChatStore(SID).getState().messages;
    expect(done[done.length - 1]?.content).toContain('It completed.');
    expect(debouncedSave).toHaveBeenCalledWith(SID);
    // With the outcome written the session is idle, and evictable again: it
    // is the least recently used one once a fourth session opens.
    const fourth = getRuntime('other-4');
    fourth.hydrated = true;
    fourth.lastUsed = 10;
    maybeEvictIdle();
    expect(useRuntimeIndex.getState().liveIds).not.toContain(SID);
  });

  it('records the action that ran when its continuation is refused', async () => {
    // A switch to Local mode while the card waited: the paused cloud turn is
    // refused at model resolution, after the action already ran.
    vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      const refusal = 'data: {"error": "Local mode keeps everything on this Mac.", "error_code": "LOCAL_MODE_CLOUD_MODEL"}\n\n';
      return new Response(refusal + 'data: [DONE]\n\n', {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    await act(async () => {
      finishAction!({ ok: true, output: 'built' });
      await answered;
    });
    const msgs = getChatStore(SID).getState().messages;
    const row = msgs[msgs.length - 1];
    expect(row.content).toContain('terminal_run');
    expect(row.content).toContain('ran, but the turn could not continue');
    expect(row.content).toContain('Local mode keeps everything on this Mac.');
    expect(row.content).toContain('It completed.');
    expect(getChatStore(SID).getState().isStreaming).toBe(false);
  });

  it('adds no action row when the continuation resumes', async () => {
    let answered: Promise<boolean> = Promise.resolve(true);
    await act(async () => {
      answered = answerApproval(approval(), true, false);
      await tick();
    });
    await act(async () => {
      finishAction!({ ok: true, output: 'built' });
      await answered;
    });
    const msgs = getChatStore(SID).getState().messages;
    expect(msgs.some((m) => m.content.includes('could not continue'))).toBe(false);
  });

  it('a message typed during the action is kept, with the real reason', async () => {
    await act(async () => {
      void answerApproval(approval(), true, false);
      await tick();
    });

    const { result } = renderHook(() => useChatStream());
    let delivered: boolean | undefined;
    await act(async () => {
      delivered = await result.current.sendMidTurn('continue');
    });

    expect(delivered).toBe(false);
    expect(chatCalls()).toHaveLength(0); // no second turn was started
    expect(getChatStore(SID).getState().messages.some((m) => m.content === 'continue')).toBe(false);
    expect(useUIStore.getState().toasts.some((t) => /approved action is still running/.test(t.message))).toBe(true);
    await act(async () => { finishAction!({ ok: true }); await tick(); await tick(); });
  });

  it('refuses to resume into a session another stream owns, and keeps the card', async () => {
    registerStreamController(SID, new AbortController());

    let answered: boolean | undefined;
    await act(async () => {
      answered = await answerApproval(approval(), true, false);
    });

    expect(answered).toBe(false);
    expect(executeApproval).not.toHaveBeenCalled();
    expect(getChatStore(SID).getState().currentApproval?.toolUseId).toBe('tu_dev');
    killSessionStream(SID);
  });
});
