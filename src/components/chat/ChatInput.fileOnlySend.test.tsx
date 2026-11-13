/**
 * Attached files send with no text.
 *
 * The files are the message: the Send button is live as soon as a file is
 * attached, and the send carries the files with an empty question. With no
 * text and no file there is still nothing to send, and files alone do not
 * join a turn that is already running (they stay in the composer).
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
import { useComposerAttachmentsStore } from '@/stores/composerAttachmentsStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { getChatStore } from '@/stores/sessionRuntimes';

const SESSION = 'fileonly';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId={SESSION} />, { wrapper });
};

const attach = async (files: Array<{ id: string; filename: string }>) => {
  await act(async () => {
    useComposerAttachmentsStore.getState().update(SESSION, files);
  });
};

const sendButton = (container: HTMLElement) =>
  container.querySelector('#chatSendBtn') as HTMLButtonElement;

describe('ChatInput with files and no text', () => {
  beforeEach(() => {
    h.send.mockClear();
    h.sendMidTurn.mockClear();
    useSessionStore.setState({ currentSessionId: SESSION, liveSessions: {}, sessions: [] });
    getChatStore(SESSION).getState().finishStream();
    useComposerAttachmentsStore.getState().clearSession(SESSION);
    useSettingsStore.setState({
      models: [{ key: 'm1', name: 'Model One' }],
      selectedModel: 'm1',
      needsLocalModel: false,
    });
  });

  it('keeps Send off with no text and no file', async () => {
    const { container } = renderChatInput();
    expect(sendButton(container).disabled).toBe(true);
    await act(async () => { fireEvent.submit(container.querySelector('#chatForm') as HTMLFormElement); });
    expect(h.send).not.toHaveBeenCalled();
  });

  it('sends the attached file with an empty message', async () => {
    const { container } = renderChatInput();
    await attach([{ id: 'att-1', filename: 'report.pdf' }]);
    const btn = sendButton(container);
    expect(btn.disabled).toBe(false);
    await act(async () => { fireEvent.click(btn); });

    expect(h.send).toHaveBeenCalledTimes(1);
    const [question, opts] = h.send.mock.calls[0] as [string, { attachmentIds: string[]; attachmentNames: string[] }];
    expect(question).toBe('');
    expect(opts.attachmentIds).toEqual(['att-1']);
    expect(opts.attachmentNames).toEqual(['report.pdf']);
    // The chips left the composer with the message.
    expect(useComposerAttachmentsStore.getState().bySession[SESSION]).toEqual([]);
  });

  it('keeps the files in the composer while a turn is running', async () => {
    const { container } = renderChatInput();
    await attach([{ id: 'att-1', filename: 'report.pdf' }]);
    await act(async () => { getChatStore(SESSION).getState().startTurn('streaming'); });
    await act(async () => { fireEvent.submit(container.querySelector('#chatForm') as HTMLFormElement); });

    expect(h.send).not.toHaveBeenCalled();
    expect(h.sendMidTurn).not.toHaveBeenCalled();
    expect(useComposerAttachmentsStore.getState().bySession[SESSION]).toHaveLength(1);
  });
});
