/**
 * A voice conversation belongs to the session it started in.
 *
 * The composer used to decide "voice is on" from the global voice status, so
 * text typed in session B went into session A's call, and text typed while the
 * call was still connecting (or already ending) was cleared from the box and
 * then dropped because no socket was open.
 */
import { render, act, fireEvent, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const h = vi.hoisted(() => ({
  send: vi.fn((_question: string, _opts?: unknown): Promise<void> => Promise.resolve()),
  controller: {
    loadStatus: vi.fn(() => Promise.resolve()),
    start: vi.fn(),
    stop: vi.fn(),
    sendText: vi.fn((_text: string): boolean => true),
    cancelRuns: vi.fn(),
    hasDrainingRuns: vi.fn((_sid: string | null): boolean => false),
    resolveRequest: vi.fn(),
    setMuted: vi.fn(),
  },
}));

vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: h.send, sendMidTurn: vi.fn(), abort: vi.fn() }),
}));
vi.mock('@/services/voiceController', () => ({ voiceController: h.controller }));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { useVoiceStore } from '@/stores/voiceStore';
import { useUIStore } from '@/stores/uiStore';

const renderChatInput = (sessionId: string) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId={sessionId} />, { wrapper });
};

async function typeAndSend(container: HTMLElement, text: string): Promise<HTMLTextAreaElement> {
  const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
  await act(async () => { fireEvent.change(textarea, { target: { value: text } }); });
  await act(async () => { fireEvent.keyDown(textarea, { key: 'Enter' }); });
  return textarea;
}

describe('ChatInput with a voice conversation open', () => {
  beforeEach(() => {
    h.send.mockClear();
    h.controller.sendText.mockReset().mockReturnValue(true);
    h.controller.cancelRuns.mockClear();
    h.controller.hasDrainingRuns.mockReset().mockReturnValue(false);
    useUIStore.setState({ toasts: [] });
    useVoiceStore.getState().reset();
    // Voice runs in session A; the user chose to type there.
    useVoiceStore.getState().begin('sess-A');
    useVoiceStore.getState().setReady('m', 'tiffany');
    useVoiceStore.getState().setTyping(true);
  });

  it('another session keeps an ordinary composer and sends a normal turn', async () => {
    const { container } = renderChatInput('sess-B');
    // No voice bar and no "Voice on" chip for a call that is not this session's.
    expect(container.querySelector('#voiceChip')).toBeNull();
    const textarea = await typeAndSend(container, 'hello from B');
    expect(h.controller.sendText).not.toHaveBeenCalled();
    expect(h.send).toHaveBeenCalledTimes(1);
    expect(h.send.mock.calls[0][0]).toBe('hello from B');
    expect(textarea.value).toBe('');
  });

  it('the voice session sends typed text into its own call', async () => {
    const { container } = renderChatInput('sess-A');
    const textarea = await typeAndSend(container, 'typed into the call');
    expect(h.controller.sendText).toHaveBeenCalledWith('typed into the call');
    expect(h.send).not.toHaveBeenCalled();
    expect(textarea.value).toBe('');
  });

  it('keeps the text and says so when the call cannot take it', async () => {
    h.controller.sendText.mockReturnValue(false); // connecting or ending: no open socket
    act(() => useVoiceStore.getState().setStatus('ending'));
    const { container } = renderChatInput('sess-A');
    const textarea = await typeAndSend(container, 'do not lose me');
    expect(h.send).not.toHaveBeenCalled();
    expect(textarea.value).toBe('do not lose me');
    expect(useUIStore.getState().toasts.some((t) => t.type === 'error' && /not delivered/i.test(t.message))).toBe(true);
  });

  it('the background-work Stop cancels only this session', async () => {
    act(() => useVoiceStore.getState().reset());
    act(() => useVoiceStore.getState().adjustDraining(1));
    h.controller.hasDrainingRuns.mockImplementation((sid) => sid === 'sess-B');
    renderChatInput('sess-B');
    fireEvent.click(screen.getByTitle('Stop the background work'));
    expect(h.controller.cancelRuns).toHaveBeenCalledWith('sess-B');
    act(() => useVoiceStore.getState().adjustDraining(-1));
  });
});
