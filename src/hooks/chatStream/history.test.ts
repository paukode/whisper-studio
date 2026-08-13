import { describe, expect, it } from 'vitest';
import { buildHistoryPayload } from './history';
import type { ChatMessage } from '@/types/chat';

const msg = (over: Partial<ChatMessage>): ChatMessage => ({
  role: 'user',
  content: 'hello',
  timestamp: '2026-01-01T00:00:00Z',
  ...over,
});

describe('buildHistoryPayload', () => {
  it('carries per-message attachment ids and names so the backend can re-inject files', () => {
    const rows = buildHistoryPayload(
      [
        msg({ content: 'read this', attachmentIds: ['a1'], attachmentNames: ['spec.md'] }),
        msg({ role: 'assistant', content: 'done' }),
        msg({ content: 'follow-up (just sent, excluded)' }),
      ],
      false,
    );
    expect(rows).toEqual([
      { role: 'user', content: 'read this', attachmentIds: ['a1'], attachmentNames: ['spec.md'] },
      { role: 'assistant', content: 'done' },
    ]);
  });

  it('omits attachment keys for messages without them', () => {
    const rows = buildHistoryPayload([msg({}), msg({ role: 'assistant', content: 'r' }), msg({})], false);
    expect(rows[0]).toEqual({ role: 'user', content: 'hello' });
    expect('attachmentIds' in rows[0]).toBe(false);
  });

  it('filters UI-only roles and keeps the full list on continuations', () => {
    const rows = buildHistoryPayload(
      [
        msg({}),
        { role: 'cron_event', content: 'fired', timestamp: 't' } as unknown as ChatMessage,
        msg({ role: 'assistant', content: 'r' }),
      ],
      true,
    );
    expect(rows).toHaveLength(2);
    expect(rows.some(r => r.role === 'cron_event')).toBe(false);
  });

  it('caps to the last 40 rows', () => {
    const many = Array.from({ length: 50 }, (_, i) => msg({ content: `m${i}` }));
    const rows = buildHistoryPayload(many, true);
    expect(rows).toHaveLength(40);
    expect(rows[0].content).toBe('m10');
  });
});
