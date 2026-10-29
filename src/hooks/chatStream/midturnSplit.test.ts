/**
 * A message the user sends while a reply is streaming is read by the model at
 * the end of its current step (server/chat/engine/runner.py), and the stream
 * marks that point with a `midturn_read` frame. The reply written before it
 * and the answer to the message are two replies with the message between
 * them, not one bubble that reads like the same answer twice. A round that
 * had not started its answer stops instead (`round_discarded`): its thinking
 * goes, and only the answer with the message in it is shown.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { readSSEStream } from './sseStream';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { turnEntries } from '@/components/chat/turnEntries';
import type { ChatMessage, TurnModelSettings } from '@/types/chat';

const SETTINGS: TurnModelSettings = {
  model: 'test-model',
  effort_level: 'normal',
  verbosity: 'medium',
  brief_mode: false,
};

function sseResponse(frames: unknown[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const f of frames) controller.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
      controller.enqueue(enc.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const note = (content: string): ChatMessage => ({
  role: 'user',
  content,
  timestamp: new Date().toISOString(),
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
});

describe('a message read mid-turn', () => {
  it('ends the reply so far and puts the message between it and the answer', async () => {
    const sessionId = 'sess-midturn-split';
    const chat = getChatStore(sessionId).getState();
    chat.addMessage(note('write a recognition email'));
    chat.setStreaming(true);
    const sent = note('Dana and Miles are from my team');
    chat.queueMidTurnNote(sent);

    const result = await readSSEStream(
      sseResponse([
        { text: 'Hi Priya, Miles and Dana,' },
        { text: '\n\n' },
        { midturn_read: { text: 'Dana and Miles are from my team' } },
        { text: 'Hi Dana, Miles and Priya,' },
      ]),
      sessionId,
      new AbortController().signal,
      SETTINGS,
    );

    const state = getChatStore(sessionId).getState();
    expect(state.messages.map((m) => [m.role, m.content])).toEqual([
      ['user', 'write a recognition email'],
      ['assistant', 'Hi Priya, Miles and Dana,'],
      ['user', 'Dana and Miles are from my team'],
    ]);
    // The same message object: it is not shown twice.
    expect(state.messages[2]).toBe(sent);
    expect(state.pendingMidTurn).toEqual([]);
    // The answer to the message is what the caller commits, below it.
    expect(result.fullResponse).toBe('Hi Dana, Miles and Priya,');

    state.finishStream({ role: 'assistant', content: result.fullResponse, timestamp: new Date().toISOString() });
    const kinds = turnEntries(getChatStore(sessionId).getState().messages).map((e) =>
      e.kind === 'message' ? e.message.role : 'turn',
    );
    // Two replies, the message between them.
    expect(kinds).toEqual(['user', 'turn', 'user', 'turn']);
  });

  it('still splits the reply when the message was already placed', async () => {
    // A turn that paused on a card committed its held message at the pause;
    // the continuation reads it, and only the split is left to do.
    const sessionId = 'sess-midturn-placed';
    const result = await readSSEStream(
      sseResponse([
        { text: 'Done with the first part.' },
        { midturn_read: { text: 'also check the logs' } },
        { text: 'The logs are clean.' },
      ]),
      sessionId,
      new AbortController().signal,
      SETTINGS,
    );

    const messages = getChatStore(sessionId).getState().messages;
    expect(messages.map((m) => [m.role, m.content])).toEqual([
      ['assistant', 'Done with the first part.'],
    ]);
    expect(result.fullResponse).toBe('The logs are clean.');
  });
});

describe('a round stopped to read a message', () => {
  it('drops its thinking, so the message sits right under the request and one answer follows', async () => {
    const sessionId = 'sess-midturn-restart';
    const chat = getChatStore(sessionId).getState();
    chat.addMessage(note('write a recognition email'));
    chat.setStreaming(true);
    const sent = note('Dana and Miles are from my team');
    chat.queueMidTurnNote(sent);

    const result = await readSSEStream(
      sseResponse([
        { thinking_start: true },
        { thinking: 'Drafting for Priya, Miles and Dana' },
        { round_discarded: { reason: 'midturn' } },
        { midturn_read: { text: 'Dana and Miles are from my team' } },
        { thinking_start: true },
        { thinking: 'Dana and Miles are on the team' },
        { thinking_stop: true },
        { text: 'Hi Dana, Miles and Priya,' },
      ]),
      sessionId,
      new AbortController().signal,
      SETTINGS,
    );

    const state = getChatStore(sessionId).getState();
    // No row for the stopped round: the message follows the request.
    expect(state.messages.map((m) => [m.role, m.content])).toEqual([
      ['user', 'write a recognition email'],
      ['user', 'Dana and Miles are from my team'],
    ]);
    expect(result.thinkingText).toBe('Dana and Miles are on the team');
    expect(result.thinkingText).not.toContain('Priya');
    expect(state.currentThinkingContent).not.toContain('Priya');
    expect(result.fullResponse).toBe('Hi Dana, Miles and Priya,');
  });

  it('keeps the thinking of the rounds before it', async () => {
    const sessionId = 'sess-midturn-restart-later';
    const result = await readSSEStream(
      sseResponse([
        { thinking: 'round one thought' },
        { usage: { input_tokens: 1, output_tokens: 1 } },
        { thinking: ' round two thought' },
        { round_discarded: { reason: 'midturn' } },
      ]),
      sessionId,
      new AbortController().signal,
      SETTINGS,
    );
    expect(result.thinkingText).toBe('round one thought');
  });
});

describe('held mid-turn messages', () => {
  it('follow the last row when the stream ends without reading them', () => {
    const chat = getChatStore('sess-held-end').getState();
    chat.setStreaming(true);
    const sent = note('and the subject line?');
    chat.queueMidTurnNote(sent);
    chat.finishStream({ role: 'assistant', content: 'Here it is.', timestamp: new Date().toISOString() });

    const state = getChatStore('sess-held-end').getState();
    expect(state.messages.map((m) => m.content)).toEqual(['Here it is.', 'and the subject line?']);
    expect(state.pendingMidTurn).toEqual([]);
  });

  it('are taken by their text, else the oldest first', () => {
    const chat = getChatStore('sess-held-take').getState();
    const first = note('first');
    const second = note('second');
    chat.queueMidTurnNote(first);
    chat.queueMidTurnNote(second);

    expect(getChatStore('sess-held-take').getState().takeMidTurnNote(' second ')).toBe(second);
    expect(getChatStore('sess-held-take').getState().takeMidTurnNote('changed by the server')).toBe(first);
    expect(getChatStore('sess-held-take').getState().takeMidTurnNote('none left')).toBeUndefined();
  });

  it('leave the chat when their delivery fails, wherever they are by then', () => {
    const chat = getChatStore('sess-held-drop').getState();
    chat.setStreaming(true);
    const sent = note('not delivered');
    chat.queueMidTurnNote(sent);
    // The stream ended while the request was in flight.
    chat.finishStream();
    expect(getChatStore('sess-held-drop').getState().messages).toContain(sent);

    getChatStore('sess-held-drop').getState().dropMidTurnNote(sent);
    const state = getChatStore('sess-held-drop').getState();
    expect(state.messages).not.toContain(sent);
    expect(state.pendingMidTurn).toEqual([]);
  });
});
