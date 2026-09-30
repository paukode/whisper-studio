/**
 * `deliveries` frames through a real send: the server verifies each claimed
 * delivery before it lets the sentence through, one frame per verified
 * sentence, and the chips belong under the text that claimed them. So the
 * frames of one reply merge into that reply (by kind + target, first-seen
 * order), a card that splits the reply leaves each delivery with its own
 * text, and neither a Stop nor a pause loses one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useChatStream } from './useChatStream';
import { sendApprovalContinuation } from './chatStream/sseStream';
import { dropRuntime, getActiveChatStore, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { PendingApproval } from '@/stores/chatStore';
import type { Delivery } from '@/types/chat';

const REPORT: Delivery = {
  kind: 'file',
  target: '/Users/me/Downloads/report.html',
  label: 'report.html',
  detail: '12.4 KB, saved 19:31',
  href: '#wsfile=%2FUsers%2Fme%2FDownloads%2Freport.html&open=os',
  at: '2026-09-29T19:31:02+00:00',
};
const PUSH: Delivery = { kind: 'push', target: 'main', label: 'main', detail: 'to origin/main' };

const sse = (frame: unknown) => new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`);

function streamResponse(start: (controller: ReadableStreamDefaultController<Uint8Array>) => void): Response {
  return new Response(new ReadableStream<Uint8Array>({ start }), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** /api/chat answers with these frames; anything else 200s empty. */
function chatStreamOf(frames: unknown[]): void {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
    return streamResponse((controller) => {
      for (const f of frames) controller.enqueue(sse(f));
      controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
      controller.close();
    });
  });
}

async function sendTurn(question: string) {
  const { result } = renderHook(() => useChatStream());
  await act(async () => {
    await result.current.send(question);
  });
  return getActiveChatStore().getState();
}

beforeEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  vi.restoreAllMocks();
});

describe('deliveries frames in a turn', () => {
  it('merge into the reply: one chip per delivery, a later report replacing an earlier one in place', async () => {
    const resaved = { ...REPORT, detail: '13.0 KB, saved 19:40' };
    chatStreamOf([
      { text: 'Saved report.html to Downloads. ' },
      { deliveries: { items: [REPORT] } },
      { text: 'Pushed main.' },
      { deliveries: { items: [PUSH, resaved] } },
    ]);

    const state = await sendTurn('write the report and push it');

    const reply = state.messages[state.messages.length - 1];
    expect(reply.content).toBe('Saved report.html to Downloads. Pushed main.');
    expect(reply.deliveries).toEqual([resaved, PUSH]);
    expect(state.liveDeliveries).toEqual([]);
  });

  it('stay with the text that claimed them when a card splits the reply', async () => {
    chatStreamOf([
      { text: 'Saved report.html to Downloads.' },
      { deliveries: { items: [REPORT] } },
      {
        stop_hook_block: {
          reason: 'A delivery the reply claimed could not be verified.',
          attempt: 1,
          source: 'deliverable',
        },
      },
      { text: 'It had not been pushed. It is pushed now.' },
      { deliveries: { items: [PUSH] } },
    ]);

    const { messages } = await sendTurn('write the report and push it');

    expect(messages.map((m) => m.content)).toEqual([
      'write the report and push it',
      'Saved report.html to Downloads.',
      '',
      'It had not been pushed. It is pushed now.',
    ]);
    expect(messages[1].deliveries).toEqual([REPORT]);
    expect(messages[2].toolUse?.[0].toolName).toBe('stop_hook_block');
    expect(messages[2].deliveries).toBeUndefined();
    expect(messages[3].deliveries).toEqual([PUSH]);
  });

  it('land with their sentence when the frame comes just before it', async () => {
    chatStreamOf([
      { text: 'Writing it now.' },
      { skill: 'ws_write_file' },
      { skill_result: 'ws_write_file', output: 'Wrote report.html' },
      { deliveries: { items: [REPORT] } },
      { text: 'Saved report.html to Downloads.' },
    ]);

    const { messages } = await sendTurn('write the report');

    expect(messages[1].content).toBe('Writing it now.');
    expect(messages[1].deliveries).toBeUndefined();
    expect(messages[2].content).toBe('Saved report.html to Downloads.');
    expect(messages[2].deliveries).toEqual([REPORT]);
  });

  it('stay with the prose a question card folds in', async () => {
    chatStreamOf([
      { text: 'Saved report.html. Should I also export a PDF?' },
      { deliveries: { items: [REPORT] } },
      { user_question: { question: 'Export a PDF too?', options: ['Yes', 'No'], tool_use_id: 'tu_q' } },
    ]);

    const { messages } = await sendTurn('write the report');

    const carriers = messages.filter((m) => m.deliveries);
    expect(carriers).toHaveLength(1);
    expect(carriers[0].userQuestions?.[0].toolUseId).toBe('tu_q');
    expect(carriers[0].content).toBe('Saved report.html. Should I also export a PDF?');
    expect(carriers[0].deliveries).toEqual([REPORT]);
  });

  it('survive a Stop, on the stopped message, and only there', async () => {
    let finish: () => void = () => {};
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
      return streamResponse((controller) => {
        controller.enqueue(sse({ text: 'Saved report.html. Now the slides' }));
        controller.enqueue(sse({ deliveries: { items: [REPORT] } }));
        finish = () => {
          controller.enqueue(sse({ text: ' are done too.' }));
          controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
          controller.close();
        };
      });
    });
    const { result } = renderHook(() => useChatStream());
    let sending: Promise<void> = Promise.resolve();
    await act(async () => {
      sending = result.current.send('write the report and the slides');
      await new Promise((r) => setTimeout(r, 50));
    });
    const sid = useSessionStore.getState().currentSessionId!;
    // Shown live while the turn streams.
    expect(getChatStore(sid).getState().liveDeliveries).toEqual([REPORT]);

    act(() => result.current.abort());
    await act(async () => {
      finish();
      await sending;
    });

    const replies = getChatStore(sid).getState().messages.filter((m) => m.role === 'assistant');
    expect(replies).toHaveLength(1);
    expect(replies[0].stopped).toBe(true);
    expect(replies[0].deliveries).toEqual([REPORT]);
    expect(getChatStore(sid).getState().liveDeliveries).toEqual([]);
  });
});

describe('deliveries frames in an approval continuation', () => {
  const approval: PendingApproval = {
    toolUseId: 'tu_push',
    action: 'git_push',
    category: 'cli',
    preview: 'command',
    summary: 'Push main to origin',
    payload: { command: 'git push origin main' },
    sessionId: 'sess-cont',
    alwaysAsks: true,
    turnSettings: { model: 'test-model', effort_level: 'normal', verbosity: 'medium', brief_mode: false },
  };

  it('stay on the continuation reply while the next approval waits', async () => {
    chatStreamOf([
      { text: 'Pushed main.' },
      { deliveries: { items: [PUSH] } },
      {
        approval_request: {
          tool_use_id: 'tu_tag',
          action: 'git_push',
          category: 'cli',
          preview: 'command',
          summary: 'Push tag v1.0',
          payload: { command: 'git push origin v1.0' },
        },
      },
    ]);

    await sendApprovalContinuation(approval, 'sess-cont', true, undefined, { ok: true });

    const state = getChatStore('sess-cont').getState();
    // The next card is waiting, so the stream is still on.
    expect(state.currentApproval?.toolUseId).toBe('tu_tag');
    const reply = state.messages[state.messages.length - 1];
    expect(reply.content).toBe('Pushed main.');
    expect(reply.deliveries).toEqual([PUSH]);
    expect(state.liveDeliveries).toEqual([]);
  });
});
