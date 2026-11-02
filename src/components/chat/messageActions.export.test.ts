/**
 * A turn export lists the question cards the turn asked: the userQuestions
 * list the stream writes, and the singular userQuestion of older rows.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/utils/downloadFile', () => ({ downloadFile: vi.fn() }));

import { exportTurn, questionsOf } from './messageActions';
import { downloadFile } from '@/utils/downloadFile';
import type { ChatMessage } from '@/types/chat';

afterEach(() => vi.clearAllMocks());

const row = (extra: Partial<ChatMessage>): ChatMessage => ({
  role: 'assistant',
  content: 'Picking a store.',
  timestamp: '2026-11-02T08:00:00Z',
  ...extra,
});

describe('question cards in exports', () => {
  it('exports every question of a userQuestions row', () => {
    exportTurn([row({
      userQuestions: [
        { question: 'Which DB?', options: ['A', 'B'], toolUseId: 'tu_1' },
        { question: 'Which region?', options: ['us-east-1'], toolUseId: 'tu_2' },
      ],
    })]);
    const md = vi.mocked(downloadFile).mock.calls[0][0] as string;
    expect(md).toContain('## User Questions');
    expect(md).toContain('**Q:** Which DB?\n**Options:** A, B');
    expect(md).toContain('**Q:** Which region?\n**Options:** us-east-1');
  });

  it('reads the singular field of an older row', () => {
    const q = { question: 'Which DB?', options: ['A'], toolUseId: 'tu_1' };
    expect(questionsOf(row({ userQuestion: q }))).toEqual([q]);
    expect(questionsOf(row({}))).toEqual([]);
  });
});
