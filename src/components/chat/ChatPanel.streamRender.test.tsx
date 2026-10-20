/**
 * Streamed tokens re-render the live reply only, never the conversation.
 *
 * The panel used to subscribe to the streamed text, so every token re-ran
 * every message in the session: a 300-message session spent more time
 * re-rendering history than the stream took to arrive, and the reply fell
 * behind the network by 100 ms and more. The live bubble must still get
 * every token. Once a turn has committed a row, the live reply is the tail
 * of that turn's bubble, and the bubble must not re-render on a token either.
 * (Following the tail is a size rule now: hooks/useFollowBottom.test.tsx.)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';
import type { ChatMessage } from '@/types/chat';

const h = vi.hoisted(() => ({
  messageRenders: 0,
  turnRenders: 0,
  streamed: [] as string[],
  embedded: [] as boolean[],
  turns: [] as Array<{ noEnter?: boolean; live: boolean; rows: number }>,
  // The entrance flag of every render, by the content of the turn's first row.
  noEnterByFirst: new Map<string, boolean[]>(),
}));

vi.mock('./ChatMessage', () => ({
  ChatMessage: () => {
    h.messageRenders += 1;
    return null;
  },
}));
// The bubble draws the live tail it is handed, nothing else.
vi.mock('./TurnBubble', () => ({
  TurnBubble: ({ renderLive, noEnter, rows }: {
    renderLive?: (prior: readonly unknown[]) => unknown;
    noEnter?: boolean;
    rows: Array<{ content: string }>;
  }) => {
    h.turnRenders += 1;
    h.turns.push({ noEnter, live: !!renderLive, rows: rows.length });
    const seen = h.noEnterByFirst.get(rows[0].content) ?? [];
    h.noEnterByFirst.set(rows[0].content, [...seen, !!noEnter]);
    return renderLive ? renderLive([]) : null;
  },
}));
vi.mock('./StreamingMessage', () => ({
  StreamingMessage: ({ content, embedded }: { content: string; embedded?: boolean }) => {
    h.streamed.push(content);
    h.embedded.push(!!embedded);
    return null;
  },
}));
vi.mock('./ChatInput', () => ({ ChatInput: () => null }));
vi.mock('./ApprovalBanner', () => ({ ApprovalBanner: () => null }));
vi.mock('./GoalBanner', () => ({ GoalBanner: () => null }));
vi.mock('./AutoModeBreakerBanner', () => ({ AutoModeBreakerBanner: () => null }));
vi.mock('./VoiceLiveMessage', () => ({ VoiceLiveMessage: () => null }));
vi.mock('@/api/tasks', () => ({
  fetchSessionTasks: vi.fn(() => Promise.reject(new Error('noop'))),
}));

import { ChatPanel } from './ChatPanel';
import { useSessionStore } from '@/stores/sessionStore';
import { getChatStore } from '@/stores/sessionRuntimes';

describe('ChatPanel while a reply streams', () => {
  beforeEach(() => {
    h.messageRenders = 0;
    h.turnRenders = 0;
    h.streamed = [];
    h.embedded = [];
    h.turns = [];
    h.noEnterByFirst = new Map();
  });

  it('feeds every token to the live reply without re-rendering the messages above', async () => {
    useSessionStore.setState({ currentSessionId: 'stream-render', liveSessions: {}, sessions: [] });
    const chat = getChatStore('stream-render');
    chat.getState().setMessages([
      { role: 'user', content: 'question', timestamp: '2026-09-25T10:00:00Z' },
      { role: 'assistant', content: 'answer', timestamp: '2026-09-25T10:00:05Z' },
      { role: 'user', content: 'next', timestamp: '2026-09-25T10:01:00Z' },
    ]);
    chat.getState().setStreaming(true);
    render(<ChatPanel />);
    const rendersBefore = h.messageRenders;

    for (const token of ['Dear ', 'team, ', 'thanks.']) {
      await act(async () => {
        chat.getState().appendStreamToken(token);
      });
    }

    expect(h.messageRenders).toBe(rendersBefore);
    expect(h.streamed[h.streamed.length - 1]).toBe('Dear team, thanks.');
    // A new turn's reply has a bubble of its own.
    expect(h.embedded.every((e) => !e)).toBe(true);
  });

  it("streams a turn's tail inside its bubble without re-rendering the bubble", async () => {
    useSessionStore.setState({ currentSessionId: 'stream-render-turn', liveSessions: {}, sessions: [] });
    const chat = getChatStore('stream-render-turn');
    chat.getState().setMessages([
      { role: 'user', content: 'question', timestamp: '2026-09-25T10:00:00Z' },
      { role: 'assistant', content: 'Reading the files first.', timestamp: '2026-09-25T10:00:05Z' },
    ]);
    chat.getState().setStreaming(true);
    render(<ChatPanel />);
    const turnRendersBefore = h.turnRenders;
    const messageRendersBefore = h.messageRenders;

    for (const token of ['Found ', 'it.']) {
      await act(async () => {
        chat.getState().appendStreamToken(token);
      });
    }

    expect(h.turnRenders).toBe(turnRendersBefore);
    expect(h.messageRenders).toBe(messageRendersBefore);
    expect(h.streamed[h.streamed.length - 1]).toBe('Found it.');
    // One live reply, drawn as the turn's tail: no second bubble.
    expect(h.embedded.length).toBeGreaterThan(0);
    expect(h.embedded.every((e) => e)).toBe(true);
  });

  // The turn animated in as the live reply. Each commit swaps or grows the
  // turn's bubble, which must not replay the entrance: not when its first row
  // lands mid-turn, and not when the stream ends.
  it('marks a streamed turn no-enter from its first commit to the end of the stream', async () => {
    useSessionStore.setState({ currentSessionId: 'stream-render-enter', liveSessions: {}, sessions: [] });
    const chat = getChatStore('stream-render-enter');
    chat.getState().setMessages([
      { role: 'user', content: 'earlier', timestamp: '2026-09-25T09:00:00Z' },
      { role: 'assistant', content: 'loaded', timestamp: '2026-09-25T09:00:05Z' },
      { role: 'user', content: 'question', timestamp: '2026-09-25T10:00:00Z' },
    ]);
    chat.getState().setStreaming(true);
    render(<ChatPanel />);
    // History keeps its normal entrance.
    expect(h.turns[h.turns.length - 1]).toEqual({ noEnter: false, live: false, rows: 1 });

    await act(async () => {
      chat.getState().flushStreamSegment({ role: 'assistant', content: 'Reading first.', timestamp: '2026-09-25T10:00:05Z' });
    });
    expect(h.turns[h.turns.length - 1]).toEqual({ noEnter: true, live: true, rows: 1 });
    // Its very first render already carries the class: no one-frame blink.
    expect(h.turns.filter((t) => t.live).every((t) => t.noEnter)).toBe(true);

    await act(async () => {
      chat.getState().finishStream({ role: 'assistant', content: 'Done.', timestamp: '2026-09-25T10:00:09Z' });
    });
    expect(h.turns[h.turns.length - 1]).toEqual({ noEnter: true, live: false, rows: 2 });
  });

  // One React batch can carry two commits that start two entries (a segment
  // flush, then a card no call of the turn returns). Both bubbles replace or
  // follow the live reply, so neither replays the entrance.
  it('marks every turn a batch of commits adds, not only the last one', async () => {
    useSessionStore.setState({ currentSessionId: 'stream-render-batch', liveSessions: {}, sessions: [] });
    const chat = getChatStore('stream-render-batch');
    chat.getState().setMessages([{ role: 'user', content: 'question', timestamp: '2026-09-25T10:00:00Z' }]);
    chat.getState().setStreaming(true);
    render(<ChatPanel />);

    const watch: ChatMessage = {
      role: 'assistant', content: 'watch card', timestamp: '2026-09-25T10:00:06Z',
      toolUse: [{ toolId: 'ci_started', toolName: 'ci_started', input: { task_id: 't1' }, status: 'complete' }],
    };
    await act(async () => {
      chat.getState().flushStreamSegment({ role: 'assistant', content: 'Reading first.', timestamp: '2026-09-25T10:00:05Z' });
      chat.getState().addMessage(watch);
    });
    expect(h.noEnterByFirst.get('Reading first.')?.every(Boolean)).toBe(true);
    expect(h.noEnterByFirst.get('watch card')?.every(Boolean)).toBe(true);
  });
});

describe('the new-messages pill', () => {
  it('counts the bubbles that appeared below, not the rows a turn committed', async () => {
    useSessionStore.setState({ currentSessionId: 'unseen-pill', liveSessions: {}, sessions: [] });
    const chat = getChatStore('unseen-pill');
    const at = (s: number) => `2026-09-25T10:00:${String(s).padStart(2, '0')}Z`;
    chat.getState().setMessages([
      { role: 'user', content: 'question', timestamp: at(0) },
      { role: 'assistant', content: 'Reading first.', timestamp: at(1) },
    ]);
    const { container } = render(<ChatPanel />);
    // The user scrolled up from the bottom to read.
    const panel = container.querySelector('#chatMessages') as HTMLElement;
    Object.defineProperty(panel, 'scrollHeight', { value: 2000, configurable: true });
    Object.defineProperty(panel, 'clientHeight', { value: 400, configurable: true });
    for (const top of [1600, 0]) {
      await act(async () => {
        panel.scrollTop = top;
        panel.dispatchEvent(new Event('scroll'));
      });
    }

    // Three more rows of the same turn: one bubble grew, none appeared.
    await act(async () => {
      chat.getState().setMessages([
        ...chat.getState().messages,
        { role: 'assistant', content: '', timestamp: at(2), toolUse: [{ toolId: 'tu_1', toolName: 'ws_read_file', input: { path: 'a.ts' }, status: 'complete' }] },
        { role: 'assistant', content: 'Found it.', timestamp: at(3) },
        { role: 'assistant', content: 'Done.', timestamp: at(4) },
      ]);
    });
    expect(container.querySelector('.chat-unseen-pill')).toBeNull();

    // A cron card and the next turn: two new bubbles.
    await act(async () => {
      chat.getState().setMessages([
        ...chat.getState().messages,
        { role: 'cron_event', content: '', timestamp: at(5) },
        { role: 'assistant', content: 'The nightly run passed.', timestamp: at(6) },
      ]);
    });
    const pill = container.querySelector('.chat-unseen-pill');
    expect(pill?.textContent).toBe('2 new');
    expect(pill?.getAttribute('aria-label')).toBe('Jump to 2 new messages');
  });
});
