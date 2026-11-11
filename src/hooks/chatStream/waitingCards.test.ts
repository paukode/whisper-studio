import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import { useSessionStore } from '@/stores/sessionStore';
import type { ChatMessage } from '@/types/chat';
import { sendChatTurn } from '../useChatStream';
import { closeWaitingCards, dropWaitingCards, SKIPPED_CARD_RESULT } from './waitingCards';

const SID = 'waiting-sess';
const chat = () => getChatStore(SID).getState();

function card(toolUseId: string): PendingApproval {
  return { sessionId: SID, toolUseId, action: 'command', category: 'cli', summary: 'echo b >> two.txt' } as PendingApproval;
}

const ask: ChatMessage = { role: 'user', content: 'Run echo a, then echo b.', timestamp: 't1' };
const commandRow: ChatMessage = {
  role: 'assistant',
  content: '',
  timestamp: 't2',
  toolUse: [{ toolId: 'tu_b', toolName: 'ws_run_command', input: {}, status: 'complete' }],
};

function question(toolUseId: string, extra: Partial<NonNullable<ChatMessage['userQuestions']>[number]> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    timestamp: `q-${toolUseId}`,
    userQuestions: [{ question: 'Which fruit?', options: ['Apple', 'Banana', 'Cherry'], toolUseId, ...extra }],
  };
}

function sseDone(): Response {
  return new Response(new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(new TextEncoder().encode('data: [DONE]\n\n')); c.close(); },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function sentChatBodies(): Array<Record<string, unknown>> {
  return vi.mocked(globalThis.fetch).mock.calls
    .filter((c) => String(c[0]).includes('/api/chat'))
    .map((c) => JSON.parse(String((c[1] as RequestInit).body)) as Record<string, unknown>);
}

beforeEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => sseDone());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('closeWaitingCards', () => {
  it('closes the approval cards, says so on their rows and keeps their ids', () => {
    chat().setMessages([ask, commandRow]);
    chat().enqueueApproval(card('tu_b'));

    closeWaitingCards(SID);

    expect(chat().currentApproval).toBeNull();
    expect(chat().messages[1].toolUse?.[0]).toMatchObject({ status: 'stopped', result: SKIPPED_CARD_RESULT });
    expect(chat().skippedCards).toEqual(['tu_b']);
  });

  it('closes a question of the current turn as not answered, never one already answered or older', () => {
    chat().setMessages([
      question('tu_old'),
      { role: 'user', content: 'Something else.', timestamp: 't3' },
      question('tu_done', { answered: true, answer: 'Apple' }),
      question('tu_fruit'),
    ]);

    closeWaitingCards(SID);

    const qs = chat().messages.map((m) => m.userQuestions?.[0]);
    expect(qs[0]?.skipped).toBeUndefined();
    expect(qs[2]?.skipped).toBeUndefined();
    expect(qs[3]?.skipped).toBe(true);
    expect(chat().skippedCards).toEqual(['tu_fruit']);
  });

  it('closes a waiting folder card, and leaves an answered one alone', () => {
    const folder = (toolId: string, status: 'pending' | 'complete'): ChatMessage => ({
      role: 'assistant',
      content: '',
      timestamp: `f-${toolId}`,
      toolUse: [{ toolId, toolName: 'ws_workspace_prompt', input: {}, status }],
    });
    chat().setMessages([ask, folder('tu_done', 'complete'), folder('tu_wait', 'pending')]);

    closeWaitingCards(SID);

    expect(chat().messages[1].toolUse?.[0].status).toBe('complete');
    expect(chat().messages[2].toolUse?.[0]).toMatchObject({ status: 'stopped', result: SKIPPED_CARD_RESULT });
    expect(chat().skippedCards).toEqual(['tu_wait']);
  });

  it('does nothing when no card waits', () => {
    chat().setMessages([ask, commandRow]);
    closeWaitingCards(SID);
    expect(chat().skippedCards).toEqual([]);
  });
});

describe('a message sent while a card waits', () => {
  it('names the closed cards on its request, and forgets them once the server accepts it', async () => {
    chat().setMessages([ask, commandRow]);
    chat().enqueueApproval(card('tu_b'));

    await sendChatTurn('Stop here. Tell me what two.txt contains.', { sessionId: SID });

    expect(sentChatBodies()[0]).toMatchObject({
      question: 'Stop here. Tell me what two.txt contains.',
      skipped_cards: ['tu_b'],
      approved_tool_result: null,
    });
    expect(chat().currentApproval).toBeNull();
    expect(chat().skippedCards).toEqual([]);
  });

  it('keeps the closed cards for the next send when the request fails', async () => {
    vi.mocked(globalThis.fetch).mockImplementation(async () => new Response('down', { status: 502 }));
    chat().setMessages([ask, commandRow]);
    chat().enqueueApproval(card('tu_b'));

    await sendChatTurn('Stop here.', { sessionId: SID });

    expect(chat().skippedCards).toEqual(['tu_b']);
  });

  it('sends no closed cards with a card answer', async () => {
    chat().setMessages([ask, question('tu_fruit')]);

    await sendChatTurn('', { sessionId: SID, approvedToolResult: [{ tool_use_id: 'tu_fruit', content: 'Apple' }] });

    expect(sentChatBodies()[0].skipped_cards).toBeUndefined();
    expect(chat().messages[1].userQuestions?.[0].skipped).toBeUndefined();
  });
});

describe('dropWaitingCards', () => {
  it('forgets the cards and the closed ids without closing anything on the server', () => {
    chat().enqueueApproval(card('tu_b'));
    chat().addSkippedCards(['tu_x']);

    dropWaitingCards(SID);

    expect(chat().currentApproval).toBeNull();
    expect(chat().skippedCards).toEqual([]);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
