/**
 * The live thinking box starts closed and opens only when the user opens it.
 * The choice holds when the answer starts streaming, and a new round starts
 * closed again.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { StreamingMessage } from './StreamingMessage';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

describe('StreamingMessage thinking box', () => {
  const sid = 'sess-thinking-open';
  afterEach(() => {
    dropRuntime(sid);
    useSessionStore.setState({ currentSessionId: null });
  });

  function start() {
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid);
    chat.getState().startTurn('streaming');
    chat.setState({ currentThinkingContent: 'Reading the parser first.' });
    return chat;
  }

  it('is closed while the model thinks', () => {
    start();
    const view = render(<StreamingMessage content="" isStreaming />);
    const box = view.container.querySelector('details.thinking-block') as HTMLDetailsElement;
    expect(box.open).toBe(false);
  });

  it('stays open after the user opens it, also when the answer streams', () => {
    const chat = start();
    const view = render(<StreamingMessage content="" isStreaming />);
    const box = () => view.container.querySelector('details.thinking-block') as HTMLDetailsElement;
    box().open = true;
    fireEvent(box(), new Event('toggle'));
    expect(chat.getState().thinkingOpen).toBe(true);

    view.rerender(<StreamingMessage content="The answer" isStreaming />);
    expect(box().open).toBe(true);
  });
});
