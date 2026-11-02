/**
 * The waits (an approval card, a question, a sleep) show next to the
 * thinking time, and they stay there after the first text token arrives.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { StreamingMessage } from './StreamingMessage';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

describe('StreamingMessage wait summary', () => {
  const sid = 'sess-wait-summary';
  afterEach(() => {
    dropRuntime(sid);
    useSessionStore.setState({ currentSessionId: null });
  });

  it('keeps the waited time once text streams', () => {
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid);
    chat.getState().setStreaming(true);
    const now = performance.now();
    // Thinking began 20 minutes ago, and an approval card waited 18 of them.
    chat.setState({
      thinkingStartTime: now - 20 * 60_000,
      waitSpans: [{ kind: 'user', start: now - 19 * 60_000, end: now - 60_000 }],
      waitReasons: {},
    });
    const view = render(<StreamingMessage content="" isStreaming />);
    const waited = () => view.container.querySelector('.thinking-waited')?.textContent;
    expect(waited()).toBe('waited on you 18m');

    chat.getState().setThinkingStop();
    view.rerender(<StreamingMessage content="The answer" isStreaming />);
    expect(waited()).toBe('waited on you 18m');
  });
});
