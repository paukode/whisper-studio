/**
 * Regenerate on an assistant turn sends the question again once: the
 * history is cut at the user message (useComposerChatEvents) and the send
 * adds it back. Cut after it, the transcript showed the question twice and
 * the model received it twice, once as history and once as the question.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { useChatStream } from '@/hooks/useChatStream';
import { useComposerChatEvents } from '@/hooks/useComposerChatEvents';
import { dropRuntime, getActiveChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { TurnBubble } from '@/components/chat/TurnBubble';
import { turnEntries } from '@/components/chat/turnEntries';

function streamReplies(text: string): void {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
    const body = `data: ${JSON.stringify({ text })}\n\ndata: [DONE]\n\n`;
    return new Response(new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(new TextEncoder().encode(body)); c.close(); },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  });
}

/** The bodies of the /api/chat requests made so far. */
function chatBodies(): Array<{ question?: string; history?: Array<{ role: string; content: string }> }> {
  const calls = (globalThis.fetch as unknown as { mock: { calls: Array<[unknown, RequestInit?]> } }).mock.calls;
  return calls
    .filter((c) => String(c[0]).includes('/api/chat'))
    .map((c) => JSON.parse(String(c[1]?.body ?? '{}')));
}

const transcript = () => getActiveChatStore().getState().messages.map((m) => `${m.role}:${m.content}`);

beforeEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  vi.restoreAllMocks();
});

describe('Regenerate on an assistant turn', () => {
  it('sends the question again once and replaces the answer', async () => {
    streamReplies('First answer.');
    const { result } = renderHook(() => {
      const stream = useChatStream();
      useComposerChatEvents(stream);
      return stream;
    });
    await act(async () => { await result.current.send('Fix the flaky test'); });
    expect(transcript()).toEqual(['user:Fix the flaky test', 'assistant:First answer.']);

    const entry = turnEntries(getActiveChatStore().getState().messages).find((e) => e.kind === 'turn');
    if (entry?.kind !== 'turn') throw new Error('no turn');
    render(<TurnBubble rows={entry.rows} indices={entry.indices} />);
    vi.restoreAllMocks();
    streamReplies('Second answer.');
    await act(async () => {
      fireEvent.click(screen.getByTitle('Regenerate'));
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(transcript()).toEqual(['user:Fix the flaky test', 'assistant:Second answer.']);
    const [sent] = chatBodies();
    expect(sent.question).toBe('Fix the flaky test');
    // Nothing came before the question, so the history it carries is empty.
    expect(sent.history).toEqual([]);
  });
});
