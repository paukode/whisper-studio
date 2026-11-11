import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/sessions', () => ({ resetChatSession: vi.fn().mockResolvedValue({ reset: true }) }));
vi.mock('./streamControl', () => ({ killSessionStream: vi.fn() }));

import { resetChatSession } from '@/api/sessions';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import { useUIStore } from '@/stores/uiStore';
import { clearConversation, resetSession } from './clearConversation';
import { killSessionStream } from './streamControl';

const SID = 'clear-sess';
const chat = () => getChatStore(SID).getState();

function card(toolUseId: string): PendingApproval {
  return {
    sessionId: SID,
    toolUseId,
    action: 'command',
    category: 'cli',
    summary: 'echo b >> two.txt',
  } as PendingApproval;
}

beforeEach(() => {
  vi.mocked(resetChatSession).mockReset().mockResolvedValue({ reset: true });
  vi.mocked(killSessionStream).mockClear();
  chat().clearMessages();
  chat().dropApprovals();
  chat().setMessages([{ role: 'user', content: 'Run echo b.', timestamp: 't1' }]);
  chat().enqueueApproval(card('call-1'));
  chat().enqueueApproval(card('call-2'));
});

describe('clearConversation', () => {
  it('stops the turn, ends every card here and on the server, and empties the chat', async () => {
    expect(chat().currentApproval?.toolUseId).toBe('call-1');

    await clearConversation(SID);

    expect(killSessionStream).toHaveBeenCalledWith(SID);
    expect(chat().messages).toEqual([]);
    expect(chat().currentApproval).toBeNull();
    expect(chat().approvalQueue).toEqual([]);
    expect(resetChatSession).toHaveBeenCalledWith(SID);
  });

  it('says so when the server does not confirm, and the cards still go', async () => {
    vi.mocked(resetChatSession).mockRejectedValue(new Error('offline'));
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');

    await clearConversation(SID);

    expect(chat().currentApproval).toBeNull();
    expect(addToast).toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }));
  });
});

describe('resetSession', () => {
  it('drops the cards and keeps the messages', async () => {
    expect(await resetSession(SID)).toBe(true);
    expect(chat().currentApproval).toBeNull();
    expect(chat().approvalQueue).toEqual([]);
    expect(chat().messages).toHaveLength(1);
  });

  it('resolves false when the server call fails', async () => {
    vi.mocked(resetChatSession).mockRejectedValue(new Error('offline'));
    expect(await resetSession(SID)).toBe(false);
    expect(chat().currentApproval).toBeNull();
  });
});
