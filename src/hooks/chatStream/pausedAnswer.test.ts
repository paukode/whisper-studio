import { beforeEach, describe, expect, it } from 'vitest';
import { getChatStore } from '@/stores/sessionRuntimes';
import { markQuestionsAnswered } from './pausedAnswer';
import type { ChatMessage } from '@/types/chat';

const SID = 'paused-answer-test';
const chat = () => getChatStore(SID).getState();

const card = (): ChatMessage => ({
  role: 'assistant',
  content: '',
  timestamp: '2026-01-01T00:00:00Z',
  userQuestions: [
    { question: 'Which color?', options: ['Blue', 'Green'], toolUseId: 't1' },
    { question: 'Which size?', options: [], toolUseId: 't2' },
  ],
});

describe('markQuestionsAnswered', () => {
  beforeEach(() => chat().setMessages([card()]));

  it('keeps each answer on its question, so later turns can send it', () => {
    markQuestionsAnswered(SID, [{ tool_use_id: 't1', content: 'Green' }, { tool_use_id: 't2', content: 'Large' }], true);
    expect(chat().messages[0].userQuestions).toEqual([
      expect.objectContaining({ toolUseId: 't1', answered: true, answer: 'Green' }),
      expect.objectContaining({ toolUseId: 't2', answered: true, answer: 'Large' }),
    ]);
  });

  it('clears the answer when the resume was refused, so the card can be answered again', () => {
    const answers = [{ tool_use_id: 't1', content: 'Green' }];
    markQuestionsAnswered(SID, answers, true);
    markQuestionsAnswered(SID, answers, false);
    const [q1, q2] = chat().messages[0].userQuestions ?? [];
    expect(q1).toMatchObject({ answered: false });
    expect(q1.answer).toBeUndefined();
    expect(q2.answered).toBeUndefined();
  });

  it('marks the single-question row too', () => {
    chat().setMessages([{ role: 'assistant', content: '', timestamp: 't', userQuestion: { question: 'Why?', options: ['A'], toolUseId: 't9' } }]);
    markQuestionsAnswered(SID, [{ tool_use_id: 't9', content: 'A' }], true);
    expect(chat().messages[0].userQuestion).toMatchObject({ answered: true, answer: 'A' });
  });
});
