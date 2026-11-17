/**
 * While the model writes a large call (a whole file arrives as one call's
 * arguments), the headline names the call and its size, not "Thinking…".
 */
import { afterEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { StreamingMessage } from './StreamingMessage';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { ToolUseEvent } from '@/types/chat';

describe('StreamingMessage writing headline', () => {
  const sid = 'sess-writing-headline';
  afterEach(() => {
    dropRuntime(sid);
    useSessionStore.setState({ currentSessionId: null });
  });

  const show = (tool: Partial<ToolUseEvent>) => {
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid);
    chat.getState().startTurn('streaming');
    chat.getState().addStreamToolUse({ toolId: 'tu_w', toolName: 'ws_write_file', input: {}, status: 'running', ...tool });
    const view = render(<StreamingMessage content="" isStreaming />);
    return {
      chat,
      view,
      headline: () => view.container.querySelector('.thinking-summary')?.textContent ?? '',
    };
  };

  it('reads Thinking until the model writes a call', () => {
    const { headline, view } = show({});
    expect(headline()).toContain('Thinking…');
    expect(view.container.querySelector('.thinking-size')).toBeNull();
  });

  it('names the file and its size while the call is written', () => {
    const { headline, view } = show({
      input: { path: 'site/index.html' }, progressChars: 49152, argsStreaming: true,
    });
    expect(headline()).toContain('Writing index.html…');
    expect(headline()).not.toContain('Thinking');
    expect(view.container.querySelector('.thinking-size')?.textContent).toBe('48 KB');
    expect(view.container.querySelector('.thinking-summary .activity-spinner')).not.toBeNull();
    expect(view.container.querySelector('.thinking-summary .pulse-dot')).toBeNull();
  });

  it('names the file with no size when only the name arrived', () => {
    const { headline, view } = show({
      input: { path: 'site/index.html' }, progressChars: 28, argsStreaming: true,
    });
    expect(headline()).toContain('Writing index.html…');
    expect(view.container.querySelector('.thinking-size')).toBeNull();
    expect(view.container.querySelector('.thinking-summary .pulse-dot')).not.toBeNull();
  });

  it('says "a file" when the name has not arrived', () => {
    const { headline } = show({ progressChars: 8192, argsStreaming: true });
    expect(headline()).toContain('Writing a file…');
  });

  it('names a call that makes no file by its tool', () => {
    const { headline } = show({ toolName: 'office_script', progressChars: 8192, argsStreaming: true });
    expect(headline()).toContain('Preparing office script…');
  });

  it('goes back to Thinking when the whole call arrived', () => {
    const { headline } = show({
      input: { path: 'site/index.html' }, progressChars: 49152, argsStreaming: false,
    });
    expect(headline()).toContain('Thinking…');
  });

  it('lets a status frame outrank the writing headline', () => {
    const { chat, headline } = show({ progressChars: 8192, argsStreaming: true });
    act(() => chat.getState().setStreamStatus('Compacting the conversation…'));
    expect(headline()).toContain('Compacting the conversation…');
  });
});
