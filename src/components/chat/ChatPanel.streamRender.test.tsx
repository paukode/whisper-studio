/**
 * Streamed tokens re-render the live reply only, never the conversation.
 *
 * The panel used to subscribe to the streamed text, so every token re-ran
 * every message in the session: a 300-message session spent more time
 * re-rendering history than the stream took to arrive, and the reply fell
 * behind the network by 100 ms and more. The live bubble must still get
 * every token, and the view must still follow the tail.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';

const h = vi.hoisted(() => ({ messageRenders: 0, streamed: [] as string[] }));

vi.mock('./ChatMessage', () => ({
  ChatMessage: () => {
    h.messageRenders += 1;
    return null;
  },
}));
vi.mock('./StreamingMessage', () => ({
  StreamingMessage: ({ content }: { content: string }) => {
    h.streamed.push(content);
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
    h.streamed = [];
    Element.prototype.scrollIntoView = vi.fn();
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
    const scroll = Element.prototype.scrollIntoView as ReturnType<typeof vi.fn>;
    scroll.mockClear();

    for (const token of ['Dear ', 'team, ', 'thanks.']) {
      await act(async () => {
        chat.getState().appendStreamToken(token);
      });
    }

    expect(h.messageRenders).toBe(rendersBefore);
    expect(h.streamed[h.streamed.length - 1]).toBe('Dear team, thanks.');
    // Following the tail still happens as the text grows.
    expect(scroll).toHaveBeenCalled();
  });
});
