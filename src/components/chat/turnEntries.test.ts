import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '@/types/chat';
import { spokenTurn, traceRuns, turnEntries, turnKey, type DisplayEntry, type TurnEntry } from './turnEntries';

const tool = (name: string) => ({ toolId: `tu_${name}`, toolName: name, input: {}, status: 'complete' as const });

const user = (content: string): ChatMessage => ({ role: 'user', content, timestamp: 't' });
const text = (content: string, tools: string[] = []): ChatMessage => ({
  role: 'assistant',
  content,
  timestamp: 't',
  toolUse: tools.length ? tools.map(tool) : undefined,
});
const toolOnly = (tools: string[]): ChatMessage => ({ ...text('', tools), content: '' });
const cron = (): ChatMessage => ({ role: 'cron_event', content: '', timestamp: 't' });
const report = (): ChatMessage => ({ role: 'agent_report', content: '', timestamp: 't' });

const shape = (entries: DisplayEntry[]) =>
  entries.map((e) => (e.kind === 'turn' ? { turn: e.indices } : { message: e.index }));

describe('turnEntries', () => {
  it('folds a turn of text rows and tool-only rows into one entry, in order', () => {
    const msgs = [
      user('make the row say what it read'),
      text("I'll start with the reducer."),
      toolOnly(['ws_read_file', 'ws_read_file']),
      text('The reducer commits prose on its own.', ['ws_grep']),
      toolOnly(['ws_edit_file']),
      text('Done.'),
    ];
    const out = turnEntries(msgs);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1, 2, 3, 4, 5] }]);
  });

  it('keeps the row objects themselves and the real store indices', () => {
    const msgs = [user('q'), text('a'), toolOnly(['ws_grep']), user('next'), text('b')];
    const out = turnEntries(msgs);
    const first = out[1] as TurnEntry;
    expect(first.rows[0]).toBe(msgs[1]);
    expect(first.rows[1]).toBe(msgs[2]);
    expect(first.index).toBe(1);
    const second = out[3] as TurnEntry;
    expect(second.indices).toEqual([4]);
    expect(second.rows[0]).toBe(msgs[4]);
    // Nothing is copied or merged: every store row is still what it was.
    expect(msgs[2].toolUse).toHaveLength(1);
  });

  it('splits a turn at a cron row between two assistant rows', () => {
    const out = turnEntries([user('q'), text('a'), cron(), text('b')]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1] }, { message: 2 }, { turn: [3] }]);
  });

  it('starts a new entry at a slash-command card, which never joins the turn above it', () => {
    const card = (name: string) => toolOnly([name]);
    const out = turnEntries([
      user('q'),
      text('a'),
      card('ci_diagnosis'),
      card('workflow_preview'),
      card('ci_started'),
      card('workflow_started'),
    ]);
    expect(shape(out)).toEqual([
      { message: 0 },
      { turn: [1] },
      { turn: [2] },
      { turn: [3] },
      { turn: [4] },
      { turn: [5] },
    ]);
  });

  it('keeps the cards a model call returns in its turn: workflow_run, then a preview card, then prose', () => {
    const out = turnEntries([
      user('run the release workflow'),
      text('Starting it.', ['workflow_run']),
      toolOnly(['workflow_preview']),
      text('It waits for your approval.'),
    ]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1, 2, 3] }]);
  });

  it('keeps every card of a model-run CI autofix in its turn, in the order they landed', () => {
    const out = turnEntries([
      user('fix CI'),
      text('Looking at the failed run.', ['ci_autofix']),
      toolOnly(['workflow_preview']),
      toolOnly(['ci_diagnosis']),
      text('The fix waits for your approval.'),
    ]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1, 2, 3, 4] }]);
  });

  it('keeps a watch card in the turn whose ci_watch call returned it, across a tool-only call row', () => {
    const out = turnEntries([user('watch CI'), toolOnly(['ci_watch']), toolOnly(['ci_started']), text('Watching.')]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1, 2, 3] }]);
  });

  it('starts a new entry at a card no call before it returns', () => {
    // A slash /ci after a turn that ran a workflow: the call does not return
    // a watch card, and the turn's last row ended in prose with no call.
    const out = turnEntries([
      user('q'),
      text('Starting it.', ['workflow_run']),
      toolOnly(['ci_started']),
    ]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1] }, { turn: [2] }]);
    const later = turnEntries([
      user('q'),
      text('Starting it.', ['workflow_run']),
      toolOnly(['workflow_started']),
      text('It runs.'),
      toolOnly(['workflow_started']),
    ]);
    expect(shape(later)).toEqual([{ message: 0 }, { turn: [1, 2, 3] }, { turn: [4] }]);
  });

  it('a row with a slash card and other tools is part of the turn', () => {
    const out = turnEntries([user('q'), text('a'), toolOnly(['ws_grep', 'workflow_started'])]);
    expect(shape(out)).toEqual([{ message: 0 }, { turn: [1, 2] }]);
  });

  it('passes agent reports and the other card roles through one to one', () => {
    const answer: ChatMessage = { role: 'agent_answer', content: '', timestamp: 't' };
    const sent: ChatMessage = { role: 'session_message', content: '', timestamp: 't' };
    const out = turnEntries([text('a'), report(), report(), answer, sent, text('b')]);
    expect(shape(out)).toEqual([
      { turn: [0] },
      { message: 1 },
      { message: 2 },
      { message: 3 },
      { message: 4 },
      { turn: [5] },
    ]);
  });

  it('marks a turn spoken when any of its rows was spoken', () => {
    expect(spokenTurn([text('a'), text('b')])).toBe(false);
    expect(spokenTurn([{ ...text('Sure.'), spoken: true }, text('The written answer.')])).toBe(true);
  });

  it('keys a turn by its first row, so it keeps its key while it grows', () => {
    const a = { ...text('a'), timestamp: '2026-10-18T10:00:00Z' };
    const before = turnEntries([user('q'), a])[1] as TurnEntry;
    const after = turnEntries([user('q'), a, toolOnly(['ws_grep'])])[1] as TurnEntry;
    expect(turnKey(before)).toBe(turnKey(after));
    expect(turnKey(after)).toBe('2026-10-18T10:00:00Z-1');
  });
});

describe('traceRuns', () => {
  const thought = (tools: string[]): ChatMessage => ({ ...toolOnly(tools), _thinkingText: 'Checking first.' });

  it('puts a call and the refusal card committed after it in one run', () => {
    const rows = [text('I will clean the build.'), toolOnly(['ws_read_file', 'ws_run_command']), toolOnly(['hook_blocked']), text('The hook refused it.')];
    expect(traceRuns(rows)).toEqual([[0], [1, 2], [3]]);
  });

  it('joins tool-only rows to a row whose thought draws above its traces, and keeps a row with prose apart', () => {
    const rows = [thought(['ws_grep']), toolOnly(['ws_read_file']), toolOnly(['hook_blocked']), text('Found it.', ['ws_edit_file'])];
    expect(traceRuns(rows)).toEqual([[0, 1, 2], [3]]);
  });

  it('keeps rows apart when prose, a thought or a card sits between their traces', () => {
    expect(traceRuns([text('Read it.', ['ws_read_file']), toolOnly(['ws_grep'])])).toEqual([[0], [1]]);
    expect(traceRuns([toolOnly(['ws_read_file']), thought(['ws_grep'])])).toEqual([[0], [1]]);
    const asked: ChatMessage = { ...toolOnly(['ws_grep']), userQuestions: [{ question: 'Which?', options: ['a'], toolUseId: 'tu_q' }] };
    expect(traceRuns([asked, toolOnly(['ws_read_file'])])).toEqual([[0], [1]]);
  });

  it('never joins a row with no traces', () => {
    expect(traceRuns([text('a'), toolOnly(['ws_grep']), text('b'), text('c')])).toEqual([[0], [1], [2], [3]]);
  });
});
