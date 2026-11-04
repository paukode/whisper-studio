/**
 * A committed thinking box is closed unless the user opened it, and the
 * user's choice is saved on its row, so it holds after a reload.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { TurnBubble } from './TurnBubble';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import type { ChatMessage } from '@/types/chat';

const question: ChatMessage = { role: 'user', content: 'Why does the sniffer pick a comma?', timestamp: '2026-11-04T10:00:00.000Z' };
const answer = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  role: 'assistant',
  content: 'It counts every character in the first 4 KB.',
  timestamp: '2026-11-04T10:00:05.000Z',
  _thinkingText: 'The memo column has quoted commas.',
  _thinkingMs: 2100,
  ...over,
});

afterEach(() => {
  getActiveChatStore().getState().setMessages([]);
});

function show(row: ChatMessage) {
  getActiveChatStore().getState().setMessages([question, row]);
  const view = render(<TurnBubble rows={[row]} indices={[1]} />);
  const box = () => view.container.querySelector('details.thinking-block') as HTMLDetailsElement;
  return { view, box };
}

describe('committed thinking box', () => {
  it('is closed by default', () => {
    expect(show(answer()).box().open).toBe(false);
  });

  it('opens when its row says the user opened it', () => {
    expect(show(answer({ _thinkingOpen: true })).box().open).toBe(true);
  });

  it('saves the user opening it on the row', () => {
    const { box } = show(answer());
    box().open = true;
    fireEvent(box(), new Event('toggle'));
    expect(getActiveChatStore().getState().messages[1]._thinkingOpen).toBe(true);
  });

  it('saves the user closing it again', () => {
    const { box } = show(answer({ _thinkingOpen: true }));
    box().open = false;
    fireEvent(box(), new Event('toggle'));
    expect(getActiveChatStore().getState().messages[1]._thinkingOpen).toBeUndefined();
  });
});
