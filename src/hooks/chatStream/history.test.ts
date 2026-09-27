import { describe, expect, it } from 'vitest';
import { buildHistoryPayload } from './history';
import type { AgentReportPayload, ChatMessage } from '@/types/chat';

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

  it('ships agent reports, wake answers and session messages for the server to relabel', () => {
    const report: AgentReportPayload = {
      team_id: 't1',
      team_name: 'research',
      agents: [{ name: 'a', agent_type: 'general', task: 'look', result: 'FINDINGS', status: 'completed' }],
    };
    const answer = { text: 'The reports say it is a payments firm.' };
    const note = { from_session_id: 's2', from_title: 'Other', content: 'hi', timestamp: 't' };
    const rows = buildHistoryPayload(
      [
        msg({}),
        msg({ role: 'assistant', content: 'researching' }),
        msg({ role: 'agent_report', content: '', agentReport: report }),
        msg({ role: 'agent_answer', content: '', agentAnswer: answer }),
        msg({ role: 'session_message', content: '', sessionMessage: note }),
        msg({ content: 'any news? (just sent, excluded)' }),
      ],
      false,
    );
    expect(rows.map(r => r.role)).toEqual([
      'user',
      'assistant',
      'agent_report',
      'agent_answer',
      'session_message',
    ]);
    expect(rows[2].agentReport).toEqual(report);
    expect(rows[3].agentAnswer).toEqual(answer);
    expect(rows[4].sessionMessage).toEqual(note);
  });

  it('never lets backend rows push the conversation out of the window', () => {
    const conversation = Array.from({ length: 40 }, (_, i) =>
      msg({ role: i % 2 ? 'assistant' : 'user', content: `m${i}` }),
    );
    const reports = Array.from({ length: 10 }, () =>
      msg({ role: 'agent_report', content: '', agentReport: { team_id: 't', team_name: 'x', agents: [] } }),
    );
    const rows = buildHistoryPayload([...conversation.slice(0, 20), ...reports, ...conversation.slice(20)], true);
    expect(rows.filter(r => r.role === 'user' || r.role === 'assistant')).toHaveLength(40);
    expect(rows.filter(r => r.role === 'agent_report')).toHaveLength(10);
    expect(rows[0].content).toBe('m0');
  });

  it('caps to the last 40 rows', () => {
    const many = Array.from({ length: 50 }, (_, i) => msg({ content: `m${i}` }));
    const rows = buildHistoryPayload(many, true);
    expect(rows).toHaveLength(40);
    expect(rows[0].content).toBe('m10');
  });
});
