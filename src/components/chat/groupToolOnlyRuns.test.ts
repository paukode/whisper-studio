import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '@/types/chat';
import { groupToolOnlyRuns, isToolOnly } from './groupToolOnlyRuns';

const tool = (name: string) => ({ toolId: name, toolName: name, input: {}, status: 'complete' as const });

const user = (content: string): ChatMessage => ({ role: 'user', content, timestamp: 't' });
const text = (content: string, tools: string[] = []): ChatMessage => ({
  role: 'assistant',
  content,
  timestamp: 't',
  toolUse: tools.length ? tools.map(tool) : undefined,
});
const toolOnly = (tools: string[], teamReports?: ChatMessage['teamReports']): ChatMessage => ({
  role: 'assistant',
  content: '',
  timestamp: 't',
  toolUse: tools.map(tool),
  teamReports,
});

describe('isToolOnly', () => {
  it('is true only for an assistant row with traces or cards and nothing to read', () => {
    expect(isToolOnly(toolOnly(['ws_grep']))).toBe(true);
    expect(isToolOnly(text('hello', ['ws_grep']))).toBe(false);
    expect(isToolOnly(text('', []))).toBe(false); // nothing at all
    expect(isToolOnly({ ...toolOnly(['x']), _thinkingText: 'hmm' })).toBe(false);
    expect(isToolOnly({ ...toolOnly(['x']), programArtifact: {} as never })).toBe(false);
    expect(isToolOnly(user(''))).toBe(false);
  });
});

describe('groupToolOnlyRuns', () => {
  it('folds a run of tool-only rows into one entry, concatenating traces in order', () => {
    const msgs = [
      user('do it'),
      toolOnly(['a', 'b']),
      toolOnly(['c'], { t1: { team_id: 't1' } as never }),
      toolOnly(['d']),
      text('done'),
    ];
    const out = groupToolOnlyRuns(msgs);
    expect(out.map((e) => e.index)).toEqual([0, 1, 4]);
    expect(out[1].indices).toEqual([1, 2, 3]);
    expect(out[1].message.toolUse?.map((t) => t.toolName)).toEqual(['a', 'b', 'c', 'd']);
    expect(Object.keys(out[1].message.teamReports ?? {})).toEqual(['t1']);
    // The store messages are untouched.
    expect(msgs[1].toolUse?.length).toBe(2);
  });

  it('never merges across a row that has text, and passes everything else through', () => {
    const msgs = [toolOnly(['a']), text('no'), toolOnly(['b']), toolOnly(['c']), text('yes')];
    const out = groupToolOnlyRuns(msgs);
    expect(out.map((e) => e.index)).toEqual([0, 1, 2, 4]);
    expect(out[2].message.toolUse?.map((t) => t.toolName)).toEqual(['b', 'c']);
    expect(out[3].message.content).toBe('yes');
  });

  it('a single tool-only row is left as-is', () => {
    const out = groupToolOnlyRuns([toolOnly(['a'])]);
    expect(out).toHaveLength(1);
    expect(out[0].indices).toEqual([0]);
  });
});
