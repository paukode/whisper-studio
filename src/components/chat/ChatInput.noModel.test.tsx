/**
 * Sending with no usable chat model is refused with a reason.
 *
 * Local mode before an on-device model is installed offers no model at all.
 * A send then must not go out (the server would refuse it anyway): the user
 * gets a toast saying where to get a model, and the typed text stays put.
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
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { getChatStore } from '@/stores/sessionRuntimes';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId="nomodel" />, { wrapper });
};

const sendTyped = async (container: HTMLElement, value: string) => {
  const ta = container.querySelector('#chatInput') as HTMLTextAreaElement;
  await act(async () => { fireEvent.change(ta, { target: { value } }); });
  await act(async () => {
    fireEvent.click(container.querySelector('#chatSendBtn') as HTMLButtonElement);
  });
  return ta;
};

describe('ChatInput with no usable chat model', () => {
  beforeEach(() => {
    h.send.mockClear();
    useSessionStore.setState({ currentSessionId: 'nomodel', liveSessions: {}, sessions: [] });
    getChatStore('nomodel').getState().setStreaming(false);
    useUIStore.setState({ toasts: [] });
  });

  it('refuses in Local mode with no on-device model and says where to get one', async () => {
    useSettingsStore.setState({ models: [], selectedModel: '', needsLocalModel: true });
    const { container } = renderChatInput();
    const ta = await sendTyped(container, 'hey');
    expect(h.send).not.toHaveBeenCalled();
    const messages = useUIStore.getState().toasts.map((t) => t.message);
    expect(messages.some((m) => /Discover/.test(m))).toBe(true);
    expect(ta.value).toBe('hey');
  });

  it('sends once an on-device model is offered and selected', async () => {
    useSettingsStore.setState({
      models: [{ key: 'local_gemma', name: 'Gemma', is_local: true }],
      selectedModel: 'local_gemma',
      loadedLocalModel: 'local_gemma',
      needsLocalModel: false,
    });
    const { container } = renderChatInput();
    await sendTyped(container, 'hey');
    expect(h.send).toHaveBeenCalledTimes(1);
  });
});
