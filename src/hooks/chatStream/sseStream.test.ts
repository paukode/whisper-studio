/**
 * Unit tests for the parts of `readSSEStream` that are drivable in isolation
 * with a fake SSE ReadableStream. We feed `data:` frames straight into the
 * parser and assert the observable side effects:
 *
 *   1. A `budget_warning` frame raises a persistent, error-styled toast — the
 *      event had no handler before, so the user only saw the easy-to-miss
 *      "[Budget exceeded] …" text fallback.
 *   2. A `usage` frame reports cumulative totals, not the last round's per-round
 *      counts (which the backend always sends too, so the old precedence
 *      collapsed the counter to one round), and takes its input side from
 *      `total_prompt` rather than the cache-miss-only `total_input`.
 *
 * We only exercise return values and the (real) UI toast store; the chat store
 * is auto-created per session id by the runtime registry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readSSEStream } from './sseStream';
import { sendApprovalContinuation } from './approvalContinuation';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import type { PendingApproval } from '@/stores/chatStore';
import type { TurnModelSettings } from '@/types/chat';
import { dropRuntime, useRuntimeIndex, getChatStore } from '@/stores/sessionRuntimes';

/** The settings the request that opened a test stream carried. */
const SETTINGS: TurnModelSettings = {
  model: 'test-model',
  effort_level: 'normal',
  verbosity: 'medium',
  brief_mode: false,
};

/** Build an SSE Response from a list of frame objects, terminated by [DONE]. */
function sseResponse(frames: unknown[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const f of frames) {
        controller.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
      }
      controller.enqueue(enc.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('readSSEStream', () => {
  beforeEach(() => {
    useUIStore.setState({ toasts: [], toastQueue: [] });
  });

  afterEach(() => {
    // Drop the per-session runtime stores these streams spawned so state
    // can't leak between tests (the registry is module-scoped).
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('surfaces a budget_warning frame as a persistent error toast', async () => {
    const res = sseResponse([
      {
        budget_warning:
          'Session cost $0.5123 has reached the limit of $0.50. Start a new session.',
        budget_kind: 'session',
        budget_limit: 0.5,
        budget_current: 0.5123,
      },
    ]);

    await readSSEStream(res, 'sess-budget', new AbortController().signal, SETTINGS);

    const toast = useUIStore
      .getState()
      .toasts.find((t) => t.key === 'budget-warning');
    expect(toast).toBeTruthy();
    expect(toast!.type).toBe('error');
    // duration 0 = persistent (no auto-dismiss timer).
    expect(toast!.duration).toBe(0);
    expect(toast!.message).toContain('limit of $0.50');
  });

  it('usage frame prefers cumulative totals over per-round counts', async () => {
    const res = sseResponse([
      {
        usage: {
          input_tokens: 5,
          output_tokens: 2,
          total_input: 100,
          total_output: 40,
          estimated_cost_usd: 0.01,
        },
      },
    ]);

    const result = await readSSEStream(
      res,
      'sess-usage-total',
      new AbortController().signal,
      SETTINGS,
    );

    expect(result.inputTokens).toBe(100);
    expect(result.outputTokens).toBe(40);
  });

  it('usage frame counts prompt tokens as input, not the cache-miss remainder', async () => {
    // With prompt caching warm, total_input is the few tokens that MISSED the
    // cache; total_prompt is what was actually sent. Preferring total_input is
    // what produced the nonsensical "4 in / 4,012 out" readout.
    const res = sseResponse([
      {
        usage: {
          input_tokens: 4,
          output_tokens: 2012,
          total_input: 4,
          total_output: 2012,
          total_prompt: 118_400,
          total_cache_read: 118_000,
          total_cache_creation: 396,
          estimated_cost_usd: 0.4679,
        },
      },
    ]);

    const result = await readSSEStream(
      res,
      'sess-usage-prompt',
      new AbortController().signal,
      SETTINGS,
    );

    expect(result.inputTokens).toBe(118_400);
    expect(result.outputTokens).toBe(2012);
  });

  it('usage frame falls back to per-round counts when totals are absent', async () => {
    const res = sseResponse([
      { usage: { input_tokens: 7, output_tokens: 3 } },
    ]);

    const result = await readSSEStream(
      res,
      'sess-usage-round',
      new AbortController().signal,
      SETTINGS,
    );

    expect(result.inputTokens).toBe(7);
    expect(result.outputTokens).toBe(3);
  });

  it('ws_workspace_prompt uses the real tool_use_id as toolId, not the hardcoded placeholder', async () => {
    const res = sseResponse([
      {
        ws_workspace_prompt: {
          reason: 'no_workspace',
          tool_name: 'ws_write_file',
          suggested: '/Users/me/Documents',
          recent: [],
          tool_use_id: 'tu_abc123',
        },
      },
    ]);

    const sessionId = 'sess-ws-prompt';
    await readSSEStream(res, sessionId, new AbortController().signal, SETTINGS);

    const messages = getChatStore(sessionId).getState().messages;
    const withPrompt = messages.find((m) => m.toolUse?.some((t) => t.toolName === 'ws_workspace_prompt'));
    expect(withPrompt).toBeTruthy();
    expect(withPrompt!.toolUse![0].toolId).toBe('tu_abc123');
  });

  it('ws_workspace_prompt falls back to the hardcoded id when tool_use_id is missing', async () => {
    const res = sseResponse([
      {
        ws_workspace_prompt: {
          reason: 'no_workspace',
          suggested: '/Users/me/Documents',
          recent: [],
        },
      },
    ]);

    const sessionId = 'sess-ws-prompt-fallback';
    await readSSEStream(res, sessionId, new AbortController().signal, SETTINGS);

    const messages = getChatStore(sessionId).getState().messages;
    const withPrompt = messages.find((m) => m.toolUse?.some((t) => t.toolName === 'ws_workspace_prompt'));
    expect(withPrompt!.toolUse![0].toolId).toBe('ws_workspace_prompt');
  });

  it('commits the prose that led up to a card before the card itself', async () => {
    // Cards are appended the moment their frame arrives; the turn's text used
    // to be committed only at stream end, so the card jumped ahead of the whole
    // discussion and its Approve button ended up above everything the model
    // said about it — off-screen until the user scrolled back up.
    const res = sseResponse([
      { skill: 'workflow_run' },
      { text: "I'll set up a repair workflow. " },
      { text: 'It runs in six phases.' },
      {
        workflow_preview: {
          script: 'export const meta = {}',
          name: 'repair-ultracode-regressions-v2',
          phases: [{ title: 'reproduce' }],
        },
      },
      { text: 'Approve the card above when you are ready.' },
    ]);

    const sessionId = 'sess-card-order';
    const result = await readSSEStream(res, sessionId, new AbortController().signal, SETTINGS);

    const messages = getChatStore(sessionId).getState().messages;
    expect(messages).toHaveLength(2);
    // 1. everything streamed before the card, as its own message…
    expect(messages[0].content).toBe(
      "I'll set up a repair workflow. It runs in six phases.",
    );
    // …carrying the traces from that stretch, so they aren't lost when the
    // streaming bubble is cleared.
    expect(messages[0].toolUse?.map((t) => t.toolName)).toEqual(['workflow_run']);
    // 2. then the card.
    expect(messages[1].toolUse?.[0].toolName).toBe('workflow_preview');
    // 3. text streamed after the card stays with the caller, which commits it
    //    as the final message — below the card, in the order it was said.
    expect(result.fullResponse).toBe('Approve the card above when you are ready.');
    expect(result.flushedSegments).toBe(1);
    // The prose is committed, not still sitting in the live bubble as well.
    expect(getChatStore(sessionId).getState().currentStreamContent).toBe(
      'Approve the card above when you are ready.',
    );
  });

  it('adds no empty bubble when a card arrives before the model has said anything', async () => {
    const res = sseResponse([
      { workflow_started: { run_id: 'wf_1', name: 'nightly' } },
      { text: 'Started it.' },
    ]);

    const sessionId = 'sess-card-first';
    const result = await readSSEStream(res, sessionId, new AbortController().signal, SETTINGS);

    const messages = getChatStore(sessionId).getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].toolUse?.[0].toolName).toBe('workflow_started');
    expect(result.flushedSegments).toBe(0);
  });

  it('surfaces a status frame (mid-turn compaction notice) as an info toast', async () => {
    const res = sseResponse([
      { status: 'Compacting context (prompt too long)...' },
    ]);

    await readSSEStream(res, 'sess-status', new AbortController().signal, SETTINGS);

    const toast = useUIStore
      .getState()
      .toasts.find((t) => t.key === 'stream-status');
    expect(toast).toBeTruthy();
    expect(toast!.type).toBe('info');
    expect(toast!.message).toContain('Compacting context');
  });

  it('clears the setup status when the first tool call arrives', async () => {
    // A GPT round can open with tool calls and never emit thinking or text.
    // Only text/thinking frames used to clear the setup status, so the header
    // sat on "Waiting for the model…" through the entire tool-running phase.
    const res = sseResponse([
      { status: 'connecting' },
      { skill: 'web_search', input: {} },
    ]);

    await readSSEStream(res, 'sess-skill-status', new AbortController().signal, SETTINGS);

    expect(getChatStore('sess-skill-status').getState().streamStatus).toBeNull();
  });
});

/**
 * The approval-resume leg. Two contracts the fresh-turn path already had and
 * this one silently lacked: a turn that comes back with no text still says so,
 * and the resumed half runs on the same per-turn settings as the paused half.
 */
describe('sendApprovalContinuation', () => {
  const approval: PendingApproval = {
    toolUseId: 'tu_branch',
    action: 'git_create_branch',
    category: 'cli',
    preview: 'command',
    summary: 'Create branch docs/x from main',
    payload: { command: 'git checkout -b docs/x' },
    sessionId: 'sess-resume',
    alwaysAsks: false,
    turnSettings: { model: 'gpt5.6sol', effort_level: 'ultracode', verbosity: 'high', brief_mode: false },
  };

  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    vi.restoreAllMocks();
  });

  it('commits a visible message when the continuation returns no text', async () => {
    // The exact shape a GPT turn ending in `end_turn` with an empty output
    // produces: frames, but not one text frame. This used to commit NOTHING,
    // so an approved turn looked dead and the user had to prod the model.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([{ usage: { input_tokens: 10, output_tokens: 0 } }]),
    );

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, {
      ok: true,
      output: 'Created branch docs/x',
    });

    const messages = getChatStore('sess-resume').getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe('assistant');
    expect(messages[0].content).toContain('ended the turn without text');
    expect(getChatStore('sess-resume').getState().isStreaming).toBe(false);
  });

  it('does not call a turn silent when a card already flushed its text', async () => {
    // The turn DID answer — the text was committed early so the approval card
    // could follow it. Re-running the empty-answer fallback here printed "No
    // text response from the model" directly underneath the model's own words.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([
        { text: 'Here is the workflow I propose.' },
        { workflow_preview: { script: 'export const meta = {}', name: 'repair' } },
      ]),
    );

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, { ok: true });

    const contents = getChatStore('sess-resume').getState().messages.map((m) => m.content);
    expect(contents).toContain('Here is the workflow I propose.');
    expect(contents.some((c) => c.includes('No text response'))).toBe(false);
    expect(contents.some((c) => c.includes('ended the turn without text'))).toBe(false);
  });

  it('resumes on the settings the paused turn ran on, not on the picker', async () => {
    // The picker is window-wide: another session picked a local model and a
    // lower effort while this card waited. The resumed half must not follow.
    useSettingsStore.setState({
      selectedModel: 'local_gemma',
      effortLevel: 'low',
      verbosity: 'low',
    });
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(sseResponse([{ text: 'branch created' }]));

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, { ok: true });

    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]!.body)) as Record<string, unknown>;
    expect(body.model).toBe('gpt5.6sol');
    expect(body.effort_level).toBe('ultracode');
    expect(body.verbosity).toBe('high');
  });

  it('carries the conversation so a lost paused state still has context', async () => {
    // Normally the backend restores its stashed messages and ignores this. When
    // the app restarted while the card sat on screen, this history is the ONLY
    // context left — sending none is what made that path answer from nothing.
    const chat = getChatStore('sess-resume').getState();
    chat.addMessage({ role: 'user', content: 'make me a branch', timestamp: 't' });
    chat.addMessage({ role: 'assistant', content: "I'll create it.", timestamp: 't' });
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(sseResponse([{ text: 'done' }]));

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, { ok: true });

    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]!.body)) as {
      history: Array<{ role: string; content: string }>;
    };
    // Continuations keep the LAST row too: nothing new was appended for them.
    expect(body.history.map((r) => r.content)).toEqual([
      'make me a branch',
      "I'll create it.",
    ]);
  });

  it('describes the outcome without calling every action a filesystem write', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(sseResponse([{ text: 'done' }]));

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, {
      ok: true,
      output: 'Switched to a new branch',
    });

    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]!.body)) as {
      approved_tool_result: { content: string };
    };
    const content = body.approved_tool_result.content;
    expect(content).toContain('git_create_branch');
    expect(content).toContain('The action succeeded');
    expect(content).toContain('Switched to a new branch');
    expect(content).not.toContain('filesystem');
  });

  it('keeps a failed command’s output next to its exit code', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(sseResponse([{ text: 'done' }]));

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, {
      ok: false,
      error: 'exit code 1',
      output: '2 tests failed',
    });

    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]!.body)) as {
      approved_tool_result: { content: string };
    };
    const content = body.approved_tool_result.content;
    expect(content).toContain('FAILED');
    expect(content).toContain('exit code 1');
    expect(content).toContain('2 tests failed');
  });

  it('reports the server’s own reason when the continuation request is rejected', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'This session already has a response in progress.' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await sendApprovalContinuation(approval, 'sess-resume', true, undefined, { ok: true });

    const messages = getChatStore('sess-resume').getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toContain('already has a response in progress');
  });
});

describe('readSSEStream approval_request', () => {
  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('the card keeps the settings of the stream that paused on it', async () => {
    const sid = 'sess-card-settings';
    const opened: TurnModelSettings = {
      model: 'cloud-sonnet',
      effort_level: 'high',
      verbosity: 'medium',
      brief_mode: false,
    };
    const res = sseResponse([
      {
        approval_request: {
          tool_use_id: 'tu_1',
          action: 'terminal_run',
          category: 'cli',
          preview: 'command',
          summary: 'npm test',
          payload: { command: 'npm test' },
        },
      },
    ]);

    await readSSEStream(res, sid, new AbortController().signal, opened);
    // The picker moving afterwards changes nothing about what this card resumes on.
    useSettingsStore.setState({ selectedModel: 'local_gemma', effortLevel: 'low' });

    expect(getChatStore(sid).getState().currentApproval?.turnSettings).toEqual(opened);
  });

  it('shows a card the server asks for even when the category is on "Yes, all"', async () => {
    // The server applies the session's "Yes, all cli" itself, so a request
    // that still arrives is a floor it wants a human to answer (here: rm).
    // The client used to run it on the remembered "allow", without a click.
    const sid = 'sess-floor';
    getChatStore(sid).getState().setSessionApproval('cli', 'allow');
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const res = sseResponse([
      {
        approval_request: {
          tool_use_id: 'tu_rm',
          action: 'terminal_run',
          category: 'cli',
          preview: 'command',
          summary: 'rm -rf build',
          payload: { command: 'rm -rf build' },
        },
      },
    ]);

    await readSSEStream(res, sid, new AbortController().signal, SETTINGS);

    expect(getChatStore(sid).getState().currentApproval?.toolUseId).toBe('tu_rm');
    expect(fetchSpy.mock.calls.some((c) => String(c[0]).includes('/api/approval/execute'))).toBe(false);
    vi.restoreAllMocks();
  });

  it('the card knows which requests the server always asks for', async () => {
    const sid = 'sess-always-asks';
    const request = (id: string, always_asks?: boolean) => ({
      approval_request: {
        tool_use_id: id,
        action: 'terminal_run',
        category: 'cli',
        preview: 'command',
        summary: id,
        payload: { command: id },
        ...(always_asks === undefined ? {} : { always_asks }),
      },
    });

    await readSSEStream(
      sseResponse([request('rm -rf build', true), request('npm test', false)]),
      sid,
      new AbortController().signal,
      SETTINGS,
    );

    const chat = getChatStore(sid).getState();
    expect(chat.currentApproval?.alwaysAsks).toBe(true);
    expect(chat.approvalQueue[0]?.alwaysAsks).toBe(false);
  });
});

describe('readSSEStream skill_progress', () => {
  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('records the argument size on the running tool while a large call streams', async () => {
    const sid = 'sess-progress';
    const store = getChatStore(sid);
    const seen: Array<{ name: string; updates: Record<string, unknown> }> = [];
    const original = store.getState().updateStreamToolUse;
    store.setState({
      updateStreamToolUse: (name, updates) => {
        seen.push({ name, updates: updates as Record<string, unknown> });
        original(name, updates);
      },
    });
    const res = sseResponse([
      { skill: 'create_artifact', input: {} },
      { skill_progress: { name: 'create_artifact', chars: 8192 } },
      { skill_progress: { name: 'create_artifact', chars: 16384 } },
    ]);

    await readSSEStream(res, sid, new AbortController().signal, SETTINGS);

    const progress = seen.filter((s) => 'progressChars' in s.updates);
    expect(progress.map((s) => s.updates.progressChars)).toEqual([8192, 16384]);
    expect(progress.every((s) => s.name === 'create_artifact')).toBe(true);
  });
});

/** Where the ask_user_question frames of a stream land. */
describe('readSSEStream question grouping', () => {
  const question = (id: string, text: string) => ({
    user_question: { question: text, options: ['a', 'b'], tool_use_id: id },
  });
  const signal = () => new AbortController().signal;

  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('parallel questions of one round share a single card', async () => {
    await readSSEStream(sseResponse([question('tu_1', 'One?'), question('tu_2', 'Two?')]), 'sess-q-group', signal(), SETTINGS);

    const msgs = getChatStore('sess-q-group').getState().messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].userQuestions?.map((q) => q.toolUseId)).toEqual(['tu_1', 'tu_2']);
  });

  // The frames of one round can reach the client in separate reads. A card
  // built from the first one alone would submit on its first pick (that is
  // what a single question does) while the second question is still on the
  // wire. The card exists only once the stream has paused.
  it('the card appears once the stream has paused, with every question of the round', async () => {
    const enc = new TextEncoder();
    let push!: (chunk: string) => void;
    let close!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = (chunk) => controller.enqueue(enc.encode(chunk));
        close = () => controller.close();
      },
    });
    const res = new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    const store = getChatStore('sess-q-split');
    const reading = readSSEStream(res, 'sess-q-split', signal(), SETTINGS);

    push(`data: ${JSON.stringify(question('tu_1', 'One?'))}\n\n`);
    await new Promise((r) => setTimeout(r, 20));
    expect(store.getState().messages).toHaveLength(0);

    push(`data: ${JSON.stringify(question('tu_2', 'Two?'))}\n\ndata: [DONE]\n\n`);
    close();
    const result = await reading;

    expect(result.hasUserQuestion).toBe(true);
    const msgs = store.getState().messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].userQuestions?.map((q) => q.toolUseId)).toEqual(['tu_1', 'tu_2']);
  });

  // The field case: a question arrived on an approval continuation, the user
  // answered it (the card submitted at once, as a single question does), the
  // answer resumed the turn, and the model asked the next question. That
  // question used to be appended into the answered card, which was locked.
  it('a question asked in a later stream opens its own card', async () => {
    const store = getChatStore('sess-q-later');
    await readSSEStream(sseResponse([question('tu_1', 'One?')]), 'sess-q-later', signal(), SETTINGS);
    store.getState().setMessages(store.getState().messages.map((m) => ({
      ...m,
      userQuestions: m.userQuestions?.map((q) => ({ ...q, answered: true })),
    })));
    store.getState().setStreaming(false);

    await readSSEStream(sseResponse([question('tu_2', 'Two?')]), 'sess-q-later', signal(), SETTINGS);

    const msgs = store.getState().messages;
    expect(msgs).toHaveLength(2);
    expect(msgs[0].userQuestions?.map((q) => q.toolUseId)).toEqual(['tu_1']);
    expect(msgs[0].userQuestions?.[0].answered).toBe(true);
    expect(msgs[1].userQuestions?.map((q) => q.toolUseId)).toEqual(['tu_2']);
    expect(msgs[1].userQuestions?.[0].answered).toBeUndefined();
  });
});

/**
 * The call id on the tool frames (tool_use_id) pairs a result with its call.
 * Two same-named calls running in parallel used to be told apart by order
 * alone, and the verdict on a result was guessed from its text; the server
 * now says both (is_error, exit_code), and the client only reads them.
 */
describe('readSSEStream tool frames', () => {
  const signal = () => new AbortController().signal;
  const live = (sid: string) => getChatStore(sid).getState().currentStreamToolUse;
  const committed = (sid: string) => getChatStore(sid).getState().messages;
  // Any card commits the segment so far (flushSegment), which is where the
  // live rows become transcript rows.
  const card = { workflow_started: { run_id: 'wf_1', name: 'nightly' } };

  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('two parallel same-named reads pair their inputs and results by id', async () => {
    const sid = 'sess-pair-live';
    // Both calls start before either returns, and the second returns first.
    await readSSEStream(sseResponse([
      { skill: 'ws_read_file', input: {}, tool_use_id: 'tu_1' },
      { skill: 'ws_read_file', input: {}, tool_use_id: 'tu_2' },
      { skill_input: 'ws_read_file', input: { path: 'b.ts' }, tool_use_id: 'tu_2' },
      { skill_input: 'ws_read_file', input: { path: 'a.ts' }, tool_use_id: 'tu_1' },
      { skill_result: 'ws_read_file', output: 'B', tool_use_id: 'tu_2', is_error: false, exit_code: null },
      { skill_result: 'ws_read_file', output: 'A', tool_use_id: 'tu_1', is_error: false, exit_code: null },
    ]), sid, signal(), SETTINGS);

    expect(live(sid)).toMatchObject([
      { toolId: 'tu_1', toolName: 'ws_read_file', input: { path: 'a.ts' }, result: 'A', status: 'complete' },
      { toolId: 'tu_2', toolName: 'ws_read_file', input: { path: 'b.ts' }, result: 'B', status: 'complete' },
    ]);
  });

  it('the committed rows keep that pairing, the ids and the verdicts', async () => {
    const sid = 'sess-pair-commit';
    await readSSEStream(sseResponse([
      { skill: 'ws_read_file', tool_use_id: 'tu_1' },
      { skill: 'ws_read_file', tool_use_id: 'tu_2' },
      { skill_result: 'ws_read_file', output: 'B', tool_use_id: 'tu_2', is_error: false, exit_code: null },
      { skill_result: 'ws_read_file', output: 'A', tool_use_id: 'tu_1', is_error: false, exit_code: null },
      card,
    ]), sid, signal(), SETTINGS);

    expect(committed(sid)[0].toolUse).toMatchObject([
      { toolId: 'tu_1', toolName: 'ws_read_file', result: 'A', status: 'complete', isError: false, exitCode: null },
      { toolId: 'tu_2', toolName: 'ws_read_file', result: 'B', status: 'complete', isError: false, exitCode: null },
    ]);
  });

  it('a result the server flagged is an error row with its exit code, live and once committed', async () => {
    const frames = [
      { skill: 'ws_run_command', tool_use_id: 'tu_run' },
      {
        skill_result: 'ws_run_command',
        output: 'npm test\n2 failed\n(exit code 1)',
        tool_use_id: 'tu_run',
        is_error: true,
        exit_code: 1,
      },
    ];
    await readSSEStream(sseResponse(frames), 'sess-err-live', signal(), SETTINGS);
    expect(live('sess-err-live')[0]).toMatchObject({ toolId: 'tu_run', status: 'error', isError: true, exitCode: 1 });
    expect(getChatStore('sess-err-live').getState().lastToolError).toContain('2 failed');

    // The flush keeps the verdict: a committed row never reads 'complete'
    // for a call that failed.
    await readSSEStream(sseResponse([...frames, card]), 'sess-err-commit', signal(), SETTINGS);
    expect(committed('sess-err-commit')[0].toolUse?.[0]).toMatchObject({
      toolId: 'tu_run',
      status: 'error',
      isError: true,
      exitCode: 1,
      result: 'npm test\n2 failed\n(exit code 1)',
    });
  });

  it("the verdict is the server's, not a guess from the output text", async () => {
    // An output that opens with the word "Error" (a search for it) used to be
    // read as a failure by a regex on this side. The server's is_error decides.
    const sid = 'sess-verdict';
    await readSSEStream(sseResponse([
      { skill: 'ws_grep', tool_use_id: 'tu_g' },
      {
        skill_result: 'ws_grep',
        output: 'Error handling notes:\nsrc/a.ts:12: throw new Error(...)',
        tool_use_id: 'tu_g',
        is_error: false,
        exit_code: null,
      },
    ]), sid, signal(), SETTINGS);

    expect(live(sid)[0]).toMatchObject({ status: 'complete', isError: false, exitCode: null });
    expect(getChatStore(sid).getState().lastToolError).toBeNull();
  });

  // The meta is what the server measured about the call (server/tool_meta.py):
  // the row keeps it camelCased, live and once committed, so a chip never has
  // to read a number out of a preview that may be cut short.
  it('the meta of a result rides on the live row and on the committed row', async () => {
    const frames = [
      { skill: 'ws_write_file', tool_use_id: 'tu_m' },
      {
        skill_result: 'ws_write_file',
        output: 'Wrote a.py (240 bytes).',
        tool_use_id: 'tu_m',
        is_error: false,
        exit_code: null,
        meta: {
          duration_ms: 12,
          output_chars: 23,
          truncated: false,
          lines: 12,
          bytes: 240,
          added: 3,
          removed: 1,
        },
      },
    ];
    const meta = {
      durationMs: 12,
      outputChars: 23,
      truncated: false,
      lines: 12,
      bytes: 240,
      added: 3,
      removed: 1,
    };
    await readSSEStream(sseResponse(frames), 'sess-meta-live', signal(), SETTINGS);
    expect(live('sess-meta-live')[0].meta).toEqual(meta);

    await readSSEStream(sseResponse([...frames, card]), 'sess-meta-commit', signal(), SETTINGS);
    expect(committed('sess-meta-commit')[0].toolUse?.[0].meta).toEqual(meta);
  });

  it('a result without a meta (an older server) leaves the row without one', async () => {
    const sid = 'sess-meta-none';
    await readSSEStream(sseResponse([
      { skill: 'ws_grep', tool_use_id: 'tu_n' },
      { skill_result: 'ws_grep', output: 'a.ts:1: x', tool_use_id: 'tu_n', is_error: false, exit_code: null },
      card,
    ]), sid, signal(), SETTINGS);
    expect(committed(sid)[0].toolUse?.[0]).not.toHaveProperty('meta');
  });

  it('frames without a call id still pair by name order', async () => {
    // What an adapter that cannot tag its frames sends (and what older
    // transcripts replay): the input belongs to the call that started last,
    // each result to the first call of that name still waiting for one.
    const sid = 'sess-pair-name';
    await readSSEStream(sseResponse([
      { skill: 'task_create' },
      { skill_input: 'task_create', input: { title: 'first' } },
      { skill: 'task_create' },
      { skill_input: 'task_create', input: { title: 'second' } },
      { skill_result: 'task_create', output: 'created 1' },
      { skill_result: 'task_create', output: 'created 2' },
      card,
    ]), sid, signal(), SETTINGS);

    expect(committed(sid)[0].toolUse).toMatchObject([
      { toolId: 'task_create', input: { title: 'first' }, result: 'created 1', status: 'complete' },
      { toolId: 'task_create', input: { title: 'second' }, result: 'created 2', status: 'complete' },
    ]);
  });

  it('skill_progress keys the live update by the call id when the frame carries one', async () => {
    const sid = 'sess-progress-id';
    const store = getChatStore(sid);
    const keys: string[] = [];
    const original = store.getState().updateStreamToolUse;
    store.setState({
      updateStreamToolUse: (key, updates) => {
        if ('progressChars' in updates) keys.push(key);
        original(key, updates);
      },
    });

    await readSSEStream(sseResponse([
      { skill: 'create_artifact', input: {}, tool_use_id: 'tu_art' },
      { skill_progress: { name: 'create_artifact', chars: 8192, tool_use_id: 'tu_art' } },
    ]), sid, signal(), SETTINGS);

    expect(keys).toEqual(['tu_art']);
    expect(live(sid)[0]).toMatchObject({ toolId: 'tu_art', progressChars: 8192 });
  });

  it('an approval pause marks the call the request names, not the last running row', async () => {
    const sid = 'sess-pause-id';
    await readSSEStream(sseResponse([
      { skill: 'ws_write_file', tool_use_id: 'tu_a' },
      { skill: 'ws_read_file', tool_use_id: 'tu_b' },
      {
        approval_request: {
          tool_use_id: 'tu_a',
          action: 'ws_write_file',
          category: 'write',
          preview: 'diff',
          summary: 'Write a.ts',
          payload: { path: 'a.ts' },
        },
      },
    ]), sid, signal(), SETTINGS);

    expect(live(sid)).toMatchObject([
      { toolId: 'tu_a', status: 'complete', result: 'Awaiting approval: Write a.ts' },
      { toolId: 'tu_b', status: 'running' },
    ]);
  });

  it('an approval pause never overwrites the output of a finished same-named call', async () => {
    // Untagged frames: the first write finished, the second one paused. The
    // pause used to find the first trace by name and replace its output.
    const sid = 'sess-pause-resolved';
    await readSSEStream(sseResponse([
      { skill: 'ws_write_file' },
      { skill_result: 'ws_write_file', output: 'wrote a.ts' },
      { skill: 'ws_write_file' },
      {
        approval_request: {
          tool_use_id: 'tu_second',
          action: 'ws_write_file',
          category: 'write',
          preview: 'diff',
          summary: 'Write b.ts',
          payload: { path: 'b.ts' },
        },
      },
      card,
    ]), sid, signal(), SETTINGS);

    expect(committed(sid)[0].toolUse).toMatchObject([
      { toolName: 'ws_write_file', result: 'wrote a.ts', status: 'complete' },
      { toolName: 'ws_write_file', result: 'Awaiting approval: Write b.ts', status: 'complete' },
    ]);
  });
});
