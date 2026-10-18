/** The feed's reading order: tool results fold under their calls, the run's
 *  end is one row, and the waiting line names the tool still out. */
import { describe, expect, it } from 'vitest';
import { feedItems, waitingFor } from './AgentPanel';
import type { AgentProgressEvent } from '@/stores/agentFeedStore';

const ev = (over: Partial<AgentProgressEvent>): AgentProgressEvent => ({ agent_id: 'a1', phase: 'text', ...over }) as AgentProgressEvent;

describe('feedItems', () => {
  it('pairs each tool result with its call and keeps the rest in order', () => {
    const items = feedItems([
      ev({ seq: 1, phase: 'started' }),
      ev({ seq: 2, phase: 'turn_start', turn: 1 }),
      ev({ seq: 3, phase: 'text', text: 'reading' }),
      ev({ seq: 4, phase: 'tool_call', tool_name: 'ws_grep' }),
      ev({ seq: 5, phase: 'tool_call', tool_name: 'ws_read_file' }),
      ev({ seq: 6, phase: 'tool_result', tool_name: 'ws_read_file', output_preview: 'x' }),
      ev({ seq: 7, phase: 'tool_result', tool_name: 'ws_grep', output_preview: 'y' }),
      ev({ seq: 8, phase: 'text', text: '   ' }),
      ev({ seq: 9, phase: 'completed', turns_used: 2 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(['text', 'tool', 'tool', 'end']);
    const [, grep, read] = items;
    expect(grep.kind === 'tool' && grep.call?.tool_name).toBe('ws_grep');
    expect(grep.kind === 'tool' && grep.result?.output_preview).toBe('y');
    expect(read.kind === 'tool' && read.result?.output_preview).toBe('x');
  });

  it('keeps a result that has no call of its own', () => {
    const items = feedItems([ev({ seq: 1, phase: 'tool_result', tool_name: 'ws_grep' })]);
    expect(items).toEqual([{ kind: 'tool', call: null, result: items[0].kind === 'tool' ? items[0].result : null }]);
  });
});

describe('waitingFor', () => {
  it('names the first tool still without a result, or nothing while thinking', () => {
    expect(waitingFor([ev({ phase: 'tool_call', tool_name: 'a' }), ev({ phase: 'tool_call', tool_name: 'b' })])).toBe('a');
    expect(waitingFor([ev({ phase: 'tool_call', tool_name: 'a' }), ev({ phase: 'tool_result', tool_name: 'a' })])).toBe('');
    expect(waitingFor([])).toBe('');
  });
});
