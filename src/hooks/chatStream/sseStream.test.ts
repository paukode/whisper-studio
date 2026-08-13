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
import { readSSEStream, sendApprovalContinuation } from './sseStream';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import type { PendingApproval } from '@/stores/chatStore';
import { dropRuntime, useRuntimeIndex, getChatStore } from '@/stores/sessionRuntimes';

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

    await readSSEStream(res, 'sess-budget', new AbortController().signal);

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
    await readSSEStream(res, sessionId, new AbortController().signal);

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
    await readSSEStream(res, sessionId, new AbortController().signal);

    const messages = getChatStore(sessionId).getState().messages;
    const withPrompt = messages.find((m) => m.toolUse?.some((t) => t.toolName === 'ws_workspace_prompt'));
    expect(withPrompt!.toolUse![0].toolId).toBe('ws_workspace_prompt');
  });

  it('surfaces a status frame (mid-turn compaction notice) as an info toast', async () => {
    const res = sseResponse([
      { status: 'Compacting context (prompt too long)...' },
    ]);

    await readSSEStream(res, 'sess-status', new AbortController().signal);

    const toast = useUIStore
      .getState()
      .toasts.find((t) => t.key === 'stream-status');
    expect(toast).toBeTruthy();
    expect(toast!.type).toBe('info');
    expect(toast!.message).toContain('Compacting context');
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

  it('carries model, effort and response length so the resumed half matches', async () => {
    useSettingsStore.setState({
      selectedModel: 'gpt5.6sol',
      effortLevel: 'ultracode',
      verbosity: 'high',
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
