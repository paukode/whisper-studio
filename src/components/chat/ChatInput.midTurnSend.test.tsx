/**
 * A message still reaches the running turn while one is streaming.
 *
 * The button row keeps one button with one meaning: Stop while a turn runs,
 * Send otherwise. Sending mid-turn is Enter, and the placeholder says so,
 * which is what was missing when this read as a locked composer.
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

  it('shows one button, Stop, and no Send', async () => {
    streaming(true);
    const { container } = renderChatInput();
    expect(container.querySelector('.btn-chat-stop')).toBeTruthy();
    expect(container.querySelector('#chatSendBtn')).toBeNull();
  });

  it('Stop aborts, and does not send', async () => {
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

  it('Enter goes into the running turn, not a second turn', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    await type(ta, 'hold on, check the yml first');
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter' }); });
    expect(h.sendMidTurn).toHaveBeenCalledTimes(1);
    expect(h.send).not.toHaveBeenCalled();
  });

  it('says in the placeholder where a typed message will go', async () => {
    // The only cue that Enter still works while a turn runs, so it is a
    // contract, not decoration.
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    expect(ta.placeholder).toMatch(/running turn/i);
  });

  it('typing does not conjure a Send button next to Stop', async () => {
    streaming(true);
    const { container } = renderChatInput();
    const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
    await type(ta, 'also save it as a png');
    expect(container.querySelector('#chatSendBtn')).toBeNull();
    expect(container.querySelectorAll('.btn-chat-stop')).toHaveLength(1);
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
