/**
 * Regression test for hands-free voice submit.
 *
 * The bug: the dictation handler decided whether to submit by reading a flag
 * mutated INSIDE a setText updater, then called form.requestSubmit(). Under
 * React's automatic batching the updater does not run synchronously mid-
 * dictation, so the flag stayed false and the submit never fired even though
 * the command words were stripped from the box. These tests drive the mic
 * hook's onTranscript callback directly and assert the message is actually
 * sent, which the matcher-only unit tests could not catch.
 */
import { render, act, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const h = vi.hoisted(() => ({
  onTranscript: null as null | ((text: string, isFinal: boolean) => void),
  send: vi.fn((_question: string, _opts?: unknown): Promise<void> => Promise.resolve()),
}));

vi.mock('@/hooks/useChatInputMic', () => ({
  useChatInputMic: (opts: { onTranscript: (t: string, f: boolean) => void }) => {
    h.onTranscript = opts.onTranscript;
    return { isRecording: true, isConnecting: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() };
  },
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: h.send, abort: vi.fn() }),
}));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // ChatInput -> useSlashCommands now calls useTheme(), which requires a
  // ThemeProvider (as the real app always provides via AppProviders).
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId="s1" />, { wrapper });
};

const final = async (text: string) => {
  await act(async () => { h.onTranscript?.(text, true); });
};
const interim = async (text: string) => {
  await act(async () => { h.onTranscript?.(text, false); });
};

describe('ChatInput hands-free voice submit', () => {
  beforeEach(() => { h.send.mockClear(); });

  it('sends the accumulated dictation when a command ends the speech', async () => {
    renderChatInput();
    await final('fix the parser bug');   // dictated message
    await final('okay send');            // spoken command
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('fix the parser bug');
  });

  it('submits even when the command arrives split across fragments', async () => {
    renderChatInput();
    await final('add a dark mode toggle');
    await final('okay');   // lead only — not yet a command, accumulates
    await final('send');   // completes "okay send" at the tail
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('add a dark mode toggle');
  });

  it('does NOT submit while the user is still dictating normally', async () => {
    renderChatInput();
    await final('let me think about this for a second');
    await final('can you send it to the team later');
    expect(h.send).not.toHaveBeenCalled();
  });

  it('fires on an interim, without waiting for the final (low latency)', async () => {
    renderChatInput();
    await final('fix the parser bug');   // committed message
    await interim('okay send');          // command arrives on an INTERIM
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('fix the parser bug');
  });

  it('does not double-submit when the final repeats the command after an interim fire', async () => {
    renderChatInput();
    await final('do the thing');
    await interim('send now');   // fires here
    await final('send now');     // trailing final for the same utterance — ignored
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('do the thing');
  });

  it('does not fire on a normal interim that has no command', async () => {
    renderChatInput();
    await interim('let me describe the bug');
    await interim('let me describe the bug in detail');
    expect(h.send).not.toHaveBeenCalled();
  });

  it('still sends a typed message on Enter (form path preserved by the refactor)', async () => {
    const { container } = renderChatInput();
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    await act(async () => { fireEvent.change(textarea, { target: { value: 'hello from the keyboard' } }); });
    await act(async () => { fireEvent.keyDown(textarea, { key: 'Enter' }); });
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('hello from the keyboard');
  });
});
