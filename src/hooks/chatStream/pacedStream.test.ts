/**
 * The stream reader paces text onto the screen (./streamPacer) but never
 * loses or delays what matters: the first token paints at once, the whole
 * reply is on screen when the stream ends, and a Stop pressed while a clump
 * is still draining keeps every word that had arrived.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readSSEStream } from './sseStream';
import { killSessionStream, registerStreamController } from './streamControl';
import type { TurnModelSettings } from '@/types/chat';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';

const SETTINGS: TurnModelSettings = {
  model: 'test-model',
  effort_level: 'normal',
  verbosity: 'medium',
  brief_mode: false,
};

/** An SSE response the test feeds frame by frame. */
function liveResponse() {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({ start(c) { ctrl = c; } });
  return {
    response: new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    send: (frame: unknown) => ctrl.enqueue(enc.encode(`data: ${JSON.stringify(frame)}\n\n`)),
    end: () => { ctrl.enqueue(enc.encode('data: [DONE]\n\n')); ctrl.close(); },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 5));

describe('readSSEStream pacing', () => {
  beforeEach(() => {
    // Frames only run when the test advances them; timers stay real.
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
  });
  afterEach(() => {
    vi.useRealTimers();
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('paints the first token at once and the whole reply by the end', async () => {
    const chat = getChatStore('paced-end');
    chat.getState().setStreaming(true);
    const live = liveResponse();
    const done = readSSEStream(live.response, 'paced-end', new AbortController().signal, SETTINGS);

    live.send({ text: 'Dear' });
    await settle();
    expect(chat.getState().currentStreamContent).toBe('Dear');

    live.send({ text: ' supplier, apologies for the delay.' });
    await settle();
    // Still draining: no frame has run yet.
    expect(chat.getState().currentStreamContent).toBe('Dear');

    live.end();
    const result = await done;
    expect(result.fullResponse).toBe('Dear supplier, apologies for the delay.');
    expect(chat.getState().currentStreamContent).toBe('Dear supplier, apologies for the delay.');
  });

  it('keeps words still draining when Stop is pressed', async () => {
    const chat = getChatStore('paced-stop');
    chat.getState().setStreaming(true);
    const controller = new AbortController();
    registerStreamController('paced-stop', controller, { startsTurn: true });
    const live = liveResponse();
    const done = readSSEStream(live.response, 'paced-stop', controller.signal, SETTINGS);

    live.send({ text: 'Invoices ' });
    live.send({ text: 'for all three orders' });
    await settle();

    killSessionStream('paced-stop');
    const msgs = chat.getState().messages;
    const last = msgs[msgs.length - 1];
    expect(last?.content).toBe('Invoices for all three orders\n\n*(Stopped)*');

    live.end();
    await done.catch(() => undefined);
  });
});
