/**
 * Unit tests for the parts of `readSSEStream` that are drivable in isolation
 * with a fake SSE ReadableStream. We feed `data:` frames straight into the
 * parser and assert the observable side effects:
 *
 *   1. A `budget_warning` frame raises a persistent, error-styled toast — the
 *      event had no handler before, so the user only saw the easy-to-miss
 *      "[Budget exceeded] …" text fallback.
 *   2. A `usage` frame with cumulative `total_input`/`total_output` reports the
 *      totals, not the last round's per-round counts (which the backend always
 *      sends too, so the old precedence collapsed the counter to one round).
 *
 * We only exercise return values and the (real) UI toast store; the chat store
 * is auto-created per session id by the runtime registry.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readSSEStream } from './sseStream';
import { useUIStore } from '@/stores/uiStore';
import { dropRuntime, useRuntimeIndex } from '@/stores/sessionRuntimes';

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
    useUIStore.getState().clearToasts();
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
