/**
 * The composer stays sendable while a turn is running.
 *
 * Mid-turn messages are folded into the running turn (sendMidTurn), but the
 * composer used to SWAP Send for Stop while streaming, so the only button on
 * screen was an abort: the composer read as locked, Enter was the sole
 * undocumented way through, and the natural "that must be send" click killed
 * the turn. Stop and Send now sit side by side.
 */
import { render, act, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const h = vi.hoisted(() => ({
  send: vi.fn((_q: string, _o?: unknown): Promise<void> => Promise.resolve()),
  sendMidTurn: vi.fn((_q: string): Promise<boolean> => Promise.resolve(true)),
  abort: vi.fn(),
}));

vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: h.send, sendMidTurn: h.sendMidTurn, abort: h.abort }),
}));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { useSessionStore } from '@/stores/sessionStore';
import { getChatStore } from '@/stores/sessionRuntimes';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId="mid" />, { wrapper });
};

const streaming = (on: boolean) => {
  useSessionStore.setState({ currentSessionId: 'mid', liveSessions: {}, sessions: [] });
  getChatStore('mid').getState().setStreaming(on);
};

const type = async (el: HTMLTextAreaElement, value: string) => {
  await act(async () => { fireEvent.change(el, { target: { value } }); });
};

describe('ChatInput while a turn is streaming', () => {
  beforeEach(() => {
    h.send.mockClear();
    h.sendMidTurn.mockClear();
    h.abort.mockClear();
  });

  it('keeps Send on screen next to Stop', async () => {
    streaming(true);
    const { container } = renderChatInput();
    expect(container.querySelector('#chatSendBtn')).toBeTruthy();
    expect(container.querySelector('.btn-chat-stop')).toBeTruthy();
  });

  it('clicking Send adds the message to the running turn, and does not abort it', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    await type(ta, 'also save it as a png');

    const sendBtn = container.querySelector('#chatSendBtn') as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(false);
    await act(async () => { fireEvent.click(sendBtn); });

    expect(h.sendMidTurn).toHaveBeenCalledWith('also save it as a png');
    expect(h.send).not.toHaveBeenCalled();
    expect(h.abort).not.toHaveBeenCalled();
  });

  it('Stop still aborts, and does not send', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    await type(ta, 'never mind');
    await act(async () => {
      fireEvent.click(container.querySelector('.btn-chat-stop') as HTMLButtonElement);
    });
    expect(h.abort).toHaveBeenCalledTimes(1);
    expect(h.sendMidTurn).not.toHaveBeenCalled();
  });

  it('Enter goes to the running turn too', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    await type(ta, 'hold on, check the yml first');
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter' }); });
    expect(h.sendMidTurn).toHaveBeenCalledTimes(1);
    expect(h.send).not.toHaveBeenCalled();
  });

  it('says in the placeholder where a message will go', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    expect(ta.placeholder).toMatch(/running turn/i);
  });

  it('shows no Stop button, and sends normally, when nothing is running', async () => {
    streaming(false);
    const { container } = renderChatInput();
    expect(container.querySelector('.btn-chat-stop')).toBeNull();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    expect(ta.placeholder).not.toMatch(/running turn/i);
    await type(ta, 'start something new');
    await act(async () => {
      fireEvent.click(container.querySelector('#chatSendBtn') as HTMLButtonElement);
    });
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.sendMidTurn).not.toHaveBeenCalled();
  });
});
