/**
 * The live thinking box starts closed and opens only when the user opens it.
 * The choice holds when the answer starts streaming, and a new round starts
 * closed again. A click on its row opens and closes it while the model still
 * thinks, and the open box shows the thinking text as it arrives.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
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

  it('opens on a click while the model thinks and shows the text as it streams', () => {
    const chat = start();
    const view = render(<StreamingMessage content="" isStreaming />);
    const box = () => view.container.querySelector('details.thinking-block') as HTMLDetailsElement;
    const row = () => view.container.querySelector('.thinking-summary') as HTMLElement;
    const body = () => view.container.querySelector('.thinking-body')?.textContent;

    fireEvent.click(row());
    expect(box().open).toBe(true);
    expect(body()).toBe('Reading the parser first.');

    act(() => chat.getState().appendThinkingToken(' Then the tests.'));
    expect(box().open).toBe(true);
    expect(body()).toBe('Reading the parser first. Then the tests.');

    fireEvent.click(row());
    expect(box().open).toBe(false);
    expect(chat.getState().thinkingOpen).toBe(false);
  });

  it('opens before any thinking text arrived, and the text takes the place of the note', () => {
    const chat = start();
    chat.setState({ currentThinkingContent: '' });
    const view = render(<StreamingMessage content="" isStreaming />);
    const body = () => view.container.querySelector('.thinking-body');

    expect(body()).toBeNull();
    fireEvent.click(view.container.querySelector('.thinking-summary') as HTMLElement);
    expect((view.container.querySelector('details.thinking-block') as HTMLDetailsElement).open).toBe(true);
    const note = body()?.textContent;
    expect(note).toBeTruthy();

    act(() => chat.getState().appendThinkingToken('First thought.'));
    expect(body()?.textContent).toBe('First thought.');
    expect(body()?.textContent).not.toBe(note);
  });
});
