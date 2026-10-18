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
import { ACTIVITY_MIN_RUN, groupAgentTools, groupForActivity, isActivityEntry } from '@/components/chat/AgentCard';
import { captionIn, traceRuns, turnEntries } from '@/components/chat/turnEntries';
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

  // The paused turn committed its call's row as "Awaiting approval: ...". The
  // card's Yes runs the action outside any stream, so no skill_result ever
  // reaches that row: the leg writes the outcome onto it by tool_use_id.
  describe("the paused call's row", () => {
    const writeApproval = (): PendingApproval => ({
      ...approval(),
      action: 'ws_write_file',
      category: 'write',
      preview: 'diff',
      summary: 'Write a.py',
      payload: { path: 'a.py', content: 'x = 1\n' },
    });
    const pausedRow = {
      toolId: 'tu_dev',
      toolName: 'ws_write_file',
      input: { path: 'a.py' },
      result: 'Awaiting approval: Write a.py',
      status: 'complete' as const,
    };
    const otherRow = {
      toolId: 'tu_read',
      toolName: 'ws_read_file',
      input: { path: 'b.py' },
      result: '    1\tb',
      status: 'complete' as const,
    };
    const seed = () => getChatStore(SID).getState().setMessages([
      { role: 'user', content: 'write a.py', timestamp: 't0' },
      {
        role: 'assistant',
        content: '',
        timestamp: 't1',
        toolUse: [otherRow, pausedRow],
        traces: [{ id: 'tu_dev', name: 'ws_write_file', input: {}, output: pausedRow.result, status: 'complete' }],
      },
    ]);
    const rows = () => getChatStore(SID).getState().messages[1].toolUse ?? [];

    it('sends the call id and writes the outcome and the stats onto the row, then saves', async () => {
      seed();
      // What the row said, and whether the turn had resumed, at each save.
      const saves: Array<{ result?: string; resumed: number }> = [];
      useSessionStore.setState({
        debouncedSave: vi.fn(() => saves.push({ result: rows()[1]?.result, resumed: chatCalls().length })),
      } as never);
      let answered: Promise<boolean> = Promise.resolve(true);
      await act(async () => {
        answered = answerApproval(writeApproval(), true, false);
        await tick();
      });
      expect(executeApproval).toHaveBeenCalledWith({
        action: 'ws_write_file',
        payload: { path: 'a.py', content: 'x = 1\n' },
        tool_use_id: 'tu_dev',
      });

      await act(async () => {
        finishAction!({
          ok: true,
          output: 'Wrote a.py (6 bytes).',
          stats: { added: 3, removed: 1, lines: 12, bytes: 240, stored: true },
        });
        await answered;
      });
      const meta = {
        durationMs: null,
        outputChars: null,
        truncated: null,
        lines: null,
        bytes: 240,
        added: 3,
        removed: 1,
      };
      expect(rows()[1]).toEqual({
        ...pausedRow,
        result: 'Wrote a.py (6 bytes).',
        status: 'complete',
        isError: false,
        meta,
      });
      expect(rows()[0]).toEqual(otherRow);
      expect(getChatStore(SID).getState().messages[1].traces?.[0]).toMatchObject({
        output: 'Wrote a.py (6 bytes).',
        status: 'complete',
        meta,
      });
      // Saved with the outcome on the row, before the turn resumed.
      expect(saves[0]).toEqual({ result: 'Wrote a.py (6 bytes).', resumed: 0 });
      expect(chatCalls()).toHaveLength(1);
    });

    it('a failed action is an error row with the error the model reads, and no meta', async () => {
      seed();
      let answered: Promise<boolean> = Promise.resolve(true);
      await act(async () => {
        answered = answerApproval(writeApproval(), true, false);
        await tick();
      });
      await act(async () => {
        finishAction!({ ok: false, error: 'Write failed: disk full', output: '', stats: null });
        await answered;
      });
      expect(rows()[1]).toEqual({
        ...pausedRow,
        result: 'Error: Write failed: disk full',
        status: 'error',
        isError: true,
      });
    });

    it("keeps a preview of a long result, cut where the server cuts a live row's", async () => {
      seed();
      let answered: Promise<boolean> = Promise.resolve(true);
      await act(async () => {
        answered = answerApproval(writeApproval(), true, false);
        await tick();
      });
      await act(async () => {
        finishAction!({ ok: true, output: 'y'.repeat(5000) });
        await answered;
      });
      expect(rows()[1].result).toBe(`${'y'.repeat(2000)}...`);
      expect(rows()[1]).not.toHaveProperty('meta');
    });

    it("counts the continuation's rounds on from the paused leg's, so each round keeps its own group and caption", async () => {
      // The paused leg paused in its round 0. The server counts the
      // continuation's rounds from 0 again, and the continuation pauses
      // again with no prose, so its row is tool-only and draws in one list
      // with the paused row (traceRuns).
      getChatStore(SID).getState().setMessages([
        { role: 'user', content: 'write a.py', timestamp: 't0' },
        { role: 'assistant', content: '', timestamp: 't1', toolUse: [{ ...otherRow, round: 0 }, { ...pausedRow, round: 0 }] },
      ]);
      const frames = [
        { skill: 'ws_read_file', tool_use_id: 'tu_c', round: 0 },
        { skill_input: 'ws_read_file', input: { path: 'c.py' }, tool_use_id: 'tu_c' },
        { skill_result: 'ws_read_file', output: 'c', tool_use_id: 'tu_c', is_error: false, exit_code: null },
        { activity_label: { tool_use_id: 'tu_c', tool_use_ids: ['tu_c'], label: 'Read the caller of a.py' } },
        { skill: 'ws_write_file', tool_use_id: 'tu_w2', round: 1 },
        { skill_input: 'ws_write_file', input: { path: 'c.py' }, tool_use_id: 'tu_w2' },
        {
          approval_request: {
            tool_use_id: 'tu_w2', action: 'ws_write_file', category: 'write', preview: 'diff',
            summary: 'Write c.py', payload: { path: 'c.py', content: 'y = 2\n' },
          },
        },
      ];
      vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
        if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
        const body = [...frames.map((f) => `data: ${JSON.stringify(f)}\n\n`), 'data: [DONE]\n\n'].join('');
        return new Response(new ReadableStream<Uint8Array>({
          start(c) { c.enqueue(new TextEncoder().encode(body)); c.close(); },
        }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
      });

      let answered: Promise<boolean> = Promise.resolve(true);
      await act(async () => {
        answered = answerApproval(writeApproval(), true, false);
        await tick();
      });
      await act(async () => {
        finishAction!({ ok: true, output: 'Wrote a.py (6 bytes).' });
        await answered;
      });

      const messages = getChatStore(SID).getState().messages;
      const leg = messages[2];
      expect(leg.toolUse?.map((t) => [t.toolId, t.round])).toEqual([['tu_c', 1], ['tu_w2', 2]]);
      const turn = turnEntries(messages).find((e) => e.kind === 'turn');
      if (turn?.kind !== 'turn') throw new Error('no turn');
      const runs = traceRuns(turn.rows);
      expect(runs).toEqual([[0, 1]]);
      const drawn = runs[0].flatMap((k) => turn.rows[k].toolUse ?? []);
      const groups = groupForActivity(groupAgentTools(drawn), { minRun: ACTIVITY_MIN_RUN })
        .map((e) => (isActivityEntry(e) ? e.tools.map((t) => t.toolId) : []));
      expect(groups).toEqual([['tu_read', 'tu_dev'], ['tu_c'], ['tu_w2']]);
      // The caption names the continuation's round alone, under its own
      // first call, and the paused round keeps its deterministic label.
      expect(leg.activityLabels).toEqual({ tu_c: 'Read the caller of a.py' });
      expect(captionIn(turn.rows, 'tu_read')).toBeUndefined();
    });

    it('a Stop during the action still fills the row', async () => {
      seed();
      let answered: Promise<boolean> = Promise.resolve(true);
      await act(async () => {
        answered = answerApproval(writeApproval(), true, false);
        await tick();
      });
      act(() => killSessionStream(SID));
      await act(async () => {
        finishAction!({ ok: true, output: 'Wrote a.py (6 bytes).', stats: { added: 1, removed: 0, lines: 1, bytes: 6 } });
        await answered;
      });
      expect(rows()[1]).toMatchObject({ result: 'Wrote a.py (6 bytes).', status: 'complete', meta: { added: 1 } });
      expect(chatCalls()).toHaveLength(0);
    });
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
