import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../useChatStream', () => ({ sendChatTurn: vi.fn().mockResolvedValue(true) }));
vi.mock('./workspaceFileTurn', () => ({ askAboutWorkspaceFile: vi.fn() }));

import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import type { ChatMessage } from '@/types/chat';
import { sendChatTurn } from '../useChatStream';
import { askAboutWorkspaceFile } from './workspaceFileTurn';
import { resendUserMessage } from './resend';

const SID = 'resend-sess';
const chat = () => getChatStore(SID).getState();
const answer: ChatMessage = { role: 'assistant', content: 'An answer.', timestamp: 't2' };

function withQuestion(question: Partial<ChatMessage>): void {
  chat().setMessages([{ role: 'user', content: 'Summarize this file.', timestamp: 't1', ...question }, answer]);
}

beforeEach(() => {
  useSessionStore.setState({ currentSessionId: SID });
  vi.mocked(sendChatTurn).mockClear();
  vi.mocked(askAboutWorkspaceFile).mockClear();
});

describe('resendUserMessage', () => {
  it('sends the message again with its uploaded files, after cutting the transcript at it', () => {
    withQuestion({ attachmentIds: ['att1'], attachmentNames: ['big.pdf'] });
    resendUserMessage(0);

    expect(chat().messages).toEqual([]);
    expect(sendChatTurn).toHaveBeenCalledWith('Summarize this file.', {
      attachmentIds: ['att1'],
      attachmentNames: ['big.pdf'],
      sessionId: SID,
    });
  });

  it('sends an edited text in place of the original', () => {
    withQuestion({});
    resendUserMessage(0, 'Summarize it in one line.');
    expect(vi.mocked(sendChatTurn).mock.calls[0][0]).toBe('Summarize it in one line.');
  });

  it('uploads the workspace file again when the /file upload never finished', () => {
    withQuestion({ attachmentNames: ['big.pdf'], workspaceFile: 'docs/big.pdf' });
    resendUserMessage(0);

    expect(chat().messages).toEqual([]);
    expect(askAboutWorkspaceFile).toHaveBeenCalledWith(SID, 'docs/big.pdf', 'Summarize this file.');
    expect(sendChatTurn).not.toHaveBeenCalled();
  });

  it('never sends a message without a file it names: it says so and keeps the transcript', () => {
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');
    withQuestion({ attachmentNames: ['big.pdf'] });
    resendUserMessage(0);

    expect(sendChatTurn).not.toHaveBeenCalled();
    expect(askAboutWorkspaceFile).not.toHaveBeenCalled();
    expect(chat().messages).toHaveLength(2);
    expect(addToast).toHaveBeenCalledWith(expect.objectContaining({ type: 'error', message: expect.stringContaining('big.pdf') }));
    addToast.mockRestore();
  });
});
