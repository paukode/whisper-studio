import { describe, expect, it } from 'vitest';
import { callVerb, commandName, countFromResult, describeCall, isWholeResult, stepLabel, summariseTool } from './activityLabel';
import { callOutcome, exitCodeOf, groupChips, parseTestSummary } from './activityChips';
import type { ToolMeta, ToolUseEvent } from '@/types/chat';

const tool = (name: string, opts: Partial<ToolUseEvent> = {}): ToolUseEvent => ({
  ...opts,
  toolId: opts.toolId ?? name,
  toolName: name,
  input: opts.input ?? {},
  status: opts.status ?? 'complete',
});

const meta = (m: Partial<ToolMeta>): ToolMeta => ({
  durationMs: null, outputChars: null, truncated: null, lines: null, bytes: null, added: null, removed: null, ...m,
});

/* ── Web tool summaries ──────────────────────────────────────────────
 * The row keyed its web entries off CamelCase names this app never emits,
 * so a real web_search / web_fetch step showed an empty summary. Both
 * spellings must resolve. */
describe('web tool summaries', () => {
  it('summarises a web_search step with its query', () => {
    expect(summariseTool(tool('web_search', { input: { query: 'vite config proxy websocket' } })))
      .toBe('vite config proxy websocket');
  });

  it('summarises a web_fetch step with its URL', () => {
    expect(summariseTool(tool('web_fetch', { input: { url: 'https://example.com/docs/page' } })))
      .toBe('https://example.com/docs/page');
  });

  it('keeps the CamelCase spelling summarised the same way', () => {
    expect(summariseTool(tool('WebSearch', { input: { query: 'still labelled' } }))).toBe('still labelled');
  });

  it('names the fetched host in the call description', () => {
    expect(describeCall(tool('web_fetch', { input: { url: 'https://example.com/docs/page' } })).subject).toBe('example.com');
  });
});

/* ── Terminal summaries ──────────────────────────────────────────────
 * ws_run_command showed its command while this app's own terminal tools
 * showed nothing; they read their line from different input keys. */
describe('terminal step summaries', () => {
  it('summarises terminal_run with its command', () => {
    expect(summariseTool(tool('terminal_run', { input: { command: 'npm run build' } }))).toBe('npm run build');
  });

  it('summarises terminal_send with the line it typed', () => {
    const step = tool('terminal_send', { input: { input: 'print(len(rows))' } });
    expect(summariseTool(step)).toBe('print(len(rows))');
    expect(describeCall(step).command).toBe('print(len(rows))');
  });
});

describe('synthetic card summaries', () => {
  it('gives a Stop-hook card the generic hook label', () => {
    expect(stepLabel(tool('stop_hook_block', { input: { reason: 'tests are red' } }))).toBe('Continuing (Stop hook)');
  });

  it('gives a goal verdict in words, with the attempt when there is one', () => {
    const goal = tool('goal_eval', { input: { verdict: 'not_achieved', feedback: 'Two tests fail.', attempt: 2, cap: 8 } });
    expect(summariseTool(goal)).toBe('not achieved (2/8): Two tests fail.');
  });

  it('prefixes a blocked tool call with the tool it blocked', () => {
    const card = tool('hook_blocked', { input: { tool_name: 'ws_run_command', reason: 'rm is not allowed' } });
    expect(summariseTool(card)).toBe('ws_run_command: rm is not allowed');
  });
});

/* ── Search counts ───────────────────────────────────────────────────
 * Real outputs of server/search/grep.py and glob.py, captured from a scratch
 * workspace: src/a/index.ts holds two TODO lines, src/b/index.ts and
 * src/c.py one each. The row once counted every non-empty line, so the
 * "(3 files)" footer made a fourth match and "No matches found." made one. */
const GREP = {
  files: 'src/c.py\nsrc/b/index.ts\nsrc/a/index.ts\n(3 files)',
  filesNone: 'No matching files found.',
  filesPaged: 'src/c.py\nsrc/b/index.ts\n(showing 2 results, use offset=2 to see next page)',
  content: 'src/b/index.ts:1:TODO here\nsrc/c.py:1:x TODO\nsrc/a/index.ts:1:one TODO\nsrc/a/index.ts:3:three TODO',
  contentContext:
    'src/b/index.ts:1:TODO here\n--\nsrc/c.py:1:x TODO\n--\nsrc/a/index.ts:1:one TODO\nsrc/a/index.ts-2-two\nsrc/a/index.ts:3:three TODO',
  contentNone: 'No matches found.',
  contentPaged: 'src/b/index.ts:1:TODO here\nsrc/a/index.ts:1:one TODO\n... (showing 2 results, use offset=2 to see next page)',
  count: 'src/b/index.ts:1\nsrc/a/index.ts:2\nsrc/c.py:1\n(4 total matches across 3 files)',
  countNone: 'No matches found. (0 total across 0 files)',
};
const GLOB = {
  files: 'src/b/index.ts\nsrc/a/index.ts\n(2 files)',
  none: 'No files matched.',
  paged: 'src/c.py\nREADME.md\n(showing 2 results, use offset=2 to see next page)',
};

describe('countFromResult', () => {
  it('reads a files_with_matches grep as files, from its footer', () => {
    expect(countFromResult('ws_grep', GREP.files)).toBe('3 files');
    expect(countFromResult('ws_grep', GREP.filesNone)).toBe('0 files');
    expect(countFromResult('ws_grep', GREP.filesPaged)).toBe('2+ files');
  });

  it('counts the match rows of a content grep, never its context lines or separators', () => {
    expect(countFromResult('ws_grep', GREP.content)).toBe('4 matches');
    expect(countFromResult('ws_grep', GREP.contentContext)).toBe('4 matches');
    expect(countFromResult('ws_grep', GREP.contentNone)).toBe('0 matches');
    expect(countFromResult('ws_grep', GREP.contentPaged)).toBe('2+ matches');
  });

  it('takes a count grep total from its footer', () => {
    expect(countFromResult('ws_grep', GREP.count)).toBe('4 matches');
    expect(countFromResult('ws_grep', GREP.countNone)).toBe('0 matches');
  });

  it('reads a glob from its footer', () => {
    expect(countFromResult('ws_glob', GLOB.files)).toBe('2 files');
    expect(countFromResult('ws_glob', GLOB.none)).toBe('0 files');
    expect(countFromResult('ws_glob', GLOB.paged)).toBe('2+ files');
  });

  it('counts the entries of a listing', () => {
    expect(countFromResult('ws_list_directory', '  a/\n  b.ts  (12 bytes)\n  c.py  (3 bytes)')).toBe('3 items');
    expect(countFromResult('ws_list_directory', '(empty directory)')).toBe('0 items');
  });

  it('counts rows only from a whole preview, and a footer whenever it is there', () => {
    expect(countFromResult('ws_grep', GREP.content, false)).toBeNull();
    expect(countFromResult('ws_list_directory', '  a/\n  b.ts  (12 bytes)', false)).toBeNull();
    expect(countFromResult('ws_grep', GREP.count, false)).toBe('4 matches');
  });

  it('has no number for an error, a read, or an empty result', () => {
    expect(countFromResult('ws_grep', 'Error: pattern is required.')).toBeNull();
    expect(countFromResult('ws_grep', 'No workspace connected.')).toBeNull();
    expect(countFromResult('ws_glob', 'Search error (exit 2): bad glob')).toBeNull();
    expect(countFromResult('ws_list_directory', 'Not a directory: src/a.ts')).toBeNull();
    expect(countFromResult('ws_read_file', 'one\ntwo\nthree')).toBeNull();
    expect(countFromResult('ws_grep', '')).toBeNull();
    expect(countFromResult('ws_grep', undefined)).toBeNull();
  });

  it('puts the real count on the call and on the group', () => {
    const search = tool('ws_grep', { input: { pattern: 'TODO' }, result: GREP.files });
    expect(callOutcome(search)).toEqual({ kind: 'text', text: '3 files', tone: 'neutral' });
    expect(groupChips([search])).toEqual([{ kind: 'text', text: '3 files', tone: 'neutral' }]);
    const none = tool('ws_grep', { input: { pattern: 'ZZZ', output_mode: 'content' }, result: GREP.contentNone });
    expect(groupChips([none])).toEqual([{ kind: 'text', text: '0 matches', tone: 'neutral' }]);
  });
});

describe('isWholeResult', () => {
  it('trusts the measured length first, then the preview cut mark', () => {
    expect(isWholeResult(tool('ws_grep', { result: 'a', meta: meta({ outputChars: 1 }) }))).toBe(true);
    expect(isWholeResult(tool('ws_grep', { result: 'a'.repeat(2000) + '...', meta: meta({ outputChars: 54_000 }) }))).toBe(false);
    expect(isWholeResult(tool('ws_grep', { result: 'a', meta: meta({ outputChars: 1, truncated: true }) }))).toBe(false);
    expect(isWholeResult(tool('ws_grep', { result: 'a'.repeat(2000) + '...' }))).toBe(false);
    expect(isWholeResult(tool('ws_grep', { result: 'a.py:1:x' }))).toBe(true);
  });
});

describe('commandName', () => {
  it.each([
    ['venv/bin/ruff check .', 'ruff check'],
    ['venv/bin/python -m pytest tests/', 'python -m pytest'],
    ['cd /repo && npm test -- ActivityRow', 'npm test'],
    ['CI=1 npx tsc --noEmit', 'npx tsc'],
    ['make', 'make'],
    ['npm test | tail -5', 'npm test'],
  ])('%s fails as "%s"', (command, name) => {
    expect(commandName(command)).toBe(name);
  });
});

describe('call descriptions', () => {
  it('describes a read by its file and its verb', () => {
    const d = describeCall(tool('ws_read_file', { input: { path: 'src/types/chat.ts' } }));
    expect(d).toMatchObject({ kind: 'file', verb: 'read', subject: 'chat.ts', path: 'src/types/chat.ts' });
    expect(callVerb(tool('ws_read_file'), d)).toBe('Read');
  });

  it('describes a search by its quoted pattern and its scope', () => {
    const d = describeCall(tool('ws_grep', { input: { pattern: 'ws_auto_applied', path: 'server/' } }));
    expect(d).toMatchObject({ kind: 'search', subject: '"ws_auto_applied"', scope: 'server', scopePath: 'server/' });
  });

  it('says Running for a call that has not landed', () => {
    expect(callVerb(tool('ws_run_command', { input: { command: 'ls' }, status: 'running' }))).toBe('Running');
  });

  it('has no verb for a synthetic card, and names the card on its line', () => {
    expect(callVerb(tool('goal_eval', { input: { verdict: 'achieved' } }))).toBe('');
    const card = tool('hook_blocked', { input: { tool_name: 'ws_run_command', reason: 'rm is not allowed' }, status: 'error' });
    expect(describeCall(card).line).toBe('Blocked by hook: ws_run_command: rm is not allowed');
    expect(describeCall(tool('security_blocked')).line).toBe('Blocked (security)');
  });

  it('never takes a bare filename for a workspace path', () => {
    // save_file writes to ~/Documents or its destination; analyze_document
    // names an attachment. Neither is a file in the workspace.
    expect(describeCall(tool('save_file', { input: { filename: 'report.pdf' } })))
      .toMatchObject({ subject: 'report.pdf', line: 'report.pdf', path: null });
    expect(describeCall(tool('analyze_document', { input: { filename: 'brief.docx', question: 'Summary?' } })))
      .toMatchObject({ subject: 'brief.docx', path: null });
  });

  it('takes a save_file destination as the path only when it is absolute or under ~', () => {
    const save = (destination_path: string) => describeCall(tool('save_file', { input: { filename: 'r.pdf', destination_path } }));
    expect(save('/Users/me/Downloads/report.pdf')).toMatchObject({ path: '/Users/me/Downloads/report.pdf', subject: 'report.pdf' });
    expect(save('~/Downloads/report.pdf')).toMatchObject({ path: '~/Downloads/report.pdf', subject: 'report.pdf' });
    expect(save('out/report.pdf').path).toBeNull();
  });

  it('names a memory by its file when that is all the call carries', () => {
    expect(describeCall(tool('memory_read', { input: { filename: 'user_role.md' } })).subject).toBe('user_role.md');
    expect(describeCall(tool('memory_write', { input: { filename: 'a.md', name: 'User role' } })).subject).toBe('User role');
  });
});

describe('call outcomes', () => {
  it('a read shows the measured line count, or the range it asked for, never a count of the preview', () => {
    expect(callOutcome(tool('ws_read_file', { input: { path: 'a.ts' }, meta: meta({ lines: 1120 }), result: 'x' })))
      .toEqual({ kind: 'text', text: '1,120 lines', tone: 'neutral' });
    expect(callOutcome(tool('ws_read_file', { input: { path: 'a.ts', offset: 700, limit: 151 } })))
      .toEqual({ kind: 'text', text: 'lines 700 to 850', tone: 'neutral' });
    expect(callOutcome(tool('ws_read_file', { input: { path: 'a.ts' }, result: 'one\ntwo' }))).toBeNull();
  });

  it('a fetch shows the size of the text it returned, from the measured output', () => {
    expect(callOutcome(tool('web_fetch', { input: { url: 'https://example.com/a' }, meta: meta({ outputChars: 12800 }), result: 'x' })))
      .toEqual({ kind: 'text', text: '13 KB', tone: 'neutral' });
    expect(callOutcome(tool('web_fetch', { input: { url: 'https://example.com/a' }, meta: meta({ outputChars: 640 }), result: 'x' })))
      .toEqual({ kind: 'text', text: '640 B', tone: 'neutral' });
    expect(callOutcome(tool('web_fetch', { input: { url: 'https://example.com/a' }, result: 'x' }))).toBeNull();
  });

  it('an edit shows its diff only when both numbers are known', () => {
    expect(callOutcome(tool('ws_edit_file', { input: { path: 'a.ts' }, meta: meta({ added: 3, removed: 1 }) })))
      .toEqual({ kind: 'diff', added: 3, removed: 1 });
    expect(callOutcome(tool('ws_edit_file', { input: { path: 'a.ts' }, meta: meta({ added: 3 }) }))).toBeNull();
  });

  it('a command shows a non-zero exit in red, then a passing test summary, then exit 0', () => {
    const run = (opts: Partial<ToolUseEvent>) => tool('ws_run_command', { input: { command: 'npm test' }, ...opts });
    expect(callOutcome(run({ exitCode: 1, status: 'error', result: '3 passed, 1 failed' })))
      .toEqual({ kind: 'text', text: 'exit 1', tone: 'err' });
    expect(callOutcome(run({ exitCode: 0, result: 'Tests  424 passed (424)' })))
      .toEqual({ kind: 'text', text: '424 passed', tone: 'neutral' });
    expect(callOutcome(run({ exitCode: 0, result: 'ok' }))).toEqual({ kind: 'text', text: 'exit 0', tone: 'neutral' });
  });

  it('a stopped call did not run, whatever placeholder its result holds', () => {
    expect(callOutcome(tool('ws_grep', { input: { pattern: 'TODO' }, status: 'stopped', result: '[Stopped by user]' })))
      .toEqual({ kind: 'text', text: 'not run', tone: 'muted' });
  });

  it('a running call shows the arguments received so far', () => {
    expect(callOutcome(tool('ws_create_file', { status: 'running', progressChars: 5_120 })))
      .toEqual({ kind: 'text', text: '5.0 KB so far', tone: 'muted' });
  });
});

describe('exit codes and test summaries', () => {
  it('prefers the server exit code, else reads the trailing exit line', () => {
    expect(exitCodeOf(tool('ws_run_command', { exitCode: 3, result: '(exit code 1)' }))).toBe(3);
    expect(exitCodeOf(tool('ws_run_command', { result: 'boom\n(exit code 2)\n' }))).toBe(2);
    expect(exitCodeOf(tool('ws_run_command', { result: 'Found 1 error.\nexit 1' }))).toBe(1);
    expect(exitCodeOf(tool('ws_run_command', { result: 'fine' }))).toBeNull();
    // The server said the command has no exit status (it went to the
    // background, or never started): no chip, whatever the text says.
    const handedOff = tool('ws_run_command', { exitCode: null, result: '[Background Task Started] task_id=t1\n(exit code 0)' });
    expect(exitCodeOf(handedOff)).toBeNull();
    expect(callOutcome(handedOff)).toBeNull();
  });

  it('reads the last passed and failed counts a runner printed', () => {
    expect(parseTestSummary(' Test Files  3 passed (3)\n      Tests  424 passed (424)')).toEqual({ passed: 424, failed: 0 });
    expect(parseTestSummary('==== 2 failed, 40 passed in 3.1s ====')).toEqual({ passed: 40, failed: 2 });
    expect(parseTestSummary('no runner here')).toBeNull();
  });
});
