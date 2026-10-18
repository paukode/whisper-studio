import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActivityRow, resolvePath } from './ActivityRow';
import { useUIStore } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';
import type { ToolMeta, ToolUseEvent } from '@/types/chat';

/**
 * The activity row as the user meets it: one line that names the group, a
 * click that opens a line per call, a path that opens the file in the
 * editor, a diff pill that asks for the diff, and raw output one more click
 * away. The words and numbers are pinned in ActivityRow.test.ts; this file
 * pins what the controls do.
 */

const meta = (m: Partial<ToolMeta>): ToolMeta => ({
  durationMs: null, outputChars: null, truncated: null, lines: null, bytes: null, added: null, removed: null, ...m,
});

const call = (toolName: string, opts: Partial<ToolUseEvent> = {}): ToolUseEvent => ({
  toolId: `tu_${toolName}`,
  toolName,
  input: {},
  status: 'complete',
  ...opts,
});

const edit = call('ws_edit_file', {
  toolId: 'tu_edit',
  input: { path: 'src/components/chat/ActivityRow.tsx' },
  result: 'Applied 1 edit',
  meta: meta({ added: 48, removed: 12 }),
});
const read = call('ws_read_file', {
  toolId: 'tu_read',
  input: { path: '/repo/server/tool_executor.py' },
  result: '700  async def _apply_ws_result(call, result):',
  meta: meta({ lines: 1120, truncated: true, outputChars: 54_000 }),
});

const openFile = vi.fn();
const realOpenFile = useDockStore.getState().openFile;

beforeEach(() => {
  useUIStore.setState({ wsPath: '/repo', editDiff: null });
  useDockStore.setState({ openFile });
  openFile.mockReset();
});

afterEach(() => {
  useUIStore.setState({ wsPath: '', editDiff: null });
  useDockStore.setState({ openFile: realOpenFile });
  vi.useRealTimers();
});

const summary = (container: HTMLElement) => container.querySelector('.activity-summary') as HTMLButtonElement;

describe('ActivityRow', () => {
  it('starts collapsed as one line, and opens a line per call on click', () => {
    const { container } = render(<ActivityRow tools={[edit, read]} />);
    const head = summary(container);
    expect(head).toHaveAttribute('aria-expanded', 'false');
    expect(container.querySelector('.activity-label')?.textContent).toBe('Edited ActivityRow.tsx, read tool_executor.py');
    expect(container.querySelector('.activity-calls')).toBeNull();
    // The group pill sums the diff; it is a record there, not a control.
    const pill = container.querySelector('.activity-meta .activity-pill');
    expect(pill?.textContent).toBe('+48-12');
    expect(pill?.tagName).toBe('SPAN');

    fireEvent.click(head);
    expect(head).toHaveAttribute('aria-expanded', 'true');
    const verbs = [...container.querySelectorAll('.activity-call-verb')].map((el) => el.textContent);
    expect(verbs).toEqual(['Edited', 'Read']);
    expect(within(container.querySelector('.activity-calls') as HTMLElement).getByText('1,120 lines')).toBeInTheDocument();
  });

  it('opens a workspace path in the editor: a relative path joined to the workspace, an absolute one as is', () => {
    const { container } = render(<ActivityRow tools={[edit, read]} />);
    fireEvent.click(summary(container));
    const links = screen.getAllByTitle('Open in the editor');
    expect(links.map((l) => l.textContent)).toEqual(['src/components/chat/ActivityRow.tsx', 'server/tool_executor.py']);

    fireEvent.click(links[0]);
    expect(openFile).toHaveBeenLastCalledWith({ path: '/repo/src/components/chat/ActivityRow.tsx', title: 'ActivityRow.tsx' });
    fireEvent.click(links[1]);
    expect(openFile).toHaveBeenLastCalledWith({ path: '/repo/server/tool_executor.py', title: 'tool_executor.py' });
    // Opening a file is not a click on the line: the output stays shut.
    expect(container.querySelector('.activity-call-raw')).toBeNull();
  });

  it('links no bare filename into the workspace: save_file and analyze_document name files that live elsewhere', () => {
    const saved = call('save_file', { toolId: 'tu_save', input: { filename: 'report.pdf', content: 'x' } });
    const asked = call('analyze_document', { toolId: 'tu_doc', input: { filename: 'brief.docx', question: 'Summary?' } });
    const home = call('save_file', { toolId: 'tu_home', input: { filename: 'r.pdf', destination_path: '~/Downloads/notes.pdf' } });
    const { container } = render(<ActivityRow tools={[saved, asked, home]} />);
    fireEvent.click(summary(container));
    expect(screen.queryByTitle('Open in the editor')).toBeNull();
    const subjects = [...container.querySelectorAll('.activity-call-subject')].map((el) => el.textContent);
    expect(subjects).toEqual(['report.pdf', 'brief.docx', '~/Downloads/notes.pdf']);
  });

  it('links a save_file to its absolute destination', () => {
    const saved = call('save_file', { input: { filename: 'r.pdf', destination_path: '/Users/me/Downloads/report.pdf' } });
    const { container } = render(<ActivityRow tools={[saved]} />);
    fireEvent.click(summary(container));
    fireEvent.click(screen.getByTitle('Open in the editor'));
    expect(openFile).toHaveBeenLastCalledWith({ path: '/Users/me/Downloads/report.pdf', title: 'report.pdf' });
  });

  it('names a refusal card on its own detail line inside a mixed group', () => {
    const tools = [
      read,
      call('ws_run_command', { toolId: 'tu_rm', input: { command: 'rm -rf build' }, status: 'error', result: 'Blocked by hook' }),
      call('hook_blocked', { toolId: 'tu_hook', input: { tool_name: 'ws_run_command', reason: 'rm is not allowed' }, status: 'error' }),
    ];
    const { container } = render(<ActivityRow tools={tools} />);
    expect(container.querySelector('.activity-label')?.textContent).toBe('Blocked by hook');
    const lines = [...container.querySelectorAll('.activity-call-subject')].map((el) => el.textContent);
    expect(lines[2]).toBe('Blocked by hook: ws_run_command: rm is not allowed');
  });

  it('asks for the diff of one edit from its pill', () => {
    const { container } = render(<ActivityRow tools={[edit]} />);
    fireEvent.click(summary(container));
    fireEvent.click(screen.getByTitle('Open the diff'));
    expect(useUIStore.getState().editDiff).toEqual({ toolUseId: 'tu_edit', path: '/repo/src/components/chat/ActivityRow.tsx' });
    expect(container.querySelector('.activity-call-raw')).toBeNull();
  });

  it('shows a call output under its line, with a note when the server cut it', () => {
    const { container } = render(<ActivityRow tools={[read]} />);
    fireEvent.click(summary(container));
    const toggle = screen.getByRole('button', { name: 'Show output' });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('pre.activity-call-raw')?.textContent).toBe(read.result);
    expect(container.querySelector('.activity-call-note')?.textContent).toMatch(/of 54,000 characters/);

    // Expand all and collapse all cover every call with output.
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    expect(container.querySelector('pre.activity-call-raw')).toBeNull();
  });

  it('opens an error group with its failed output in view, red only on the marks', () => {
    const failed = call('ws_run_command', {
      input: { command: 'venv/bin/ruff check .' },
      status: 'error',
      exitCode: 1,
      result: 'E501 Line too long\nFound 1 error.',
    });
    const { container } = render(<ActivityRow tools={[failed]} />);
    expect(container.querySelector('.activity-row.activity-error.open')).not.toBeNull();
    expect(container.querySelector('.activity-head svg.activity-mark-error')).not.toBeNull();
    expect(container.querySelector('.activity-label')?.textContent).toBe('ruff check failed');
    expect(container.querySelector('.activity-chip.err')?.textContent).toBe('exit 1');
    expect(container.querySelector('.activity-call.failed pre.activity-call-raw')).not.toBeNull();
  });

  it('counts the seconds of a running group on the client clock, and drops the chip when it completes', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    const running = call('ws_run_command', { input: { command: 'venv/bin/python -m pytest tests/' }, status: 'running' });
    const { container, rerender } = render(<ActivityRow tools={[running]} />);
    expect(screen.getByRole('img', { name: 'Running' })).toBeInTheDocument();
    expect(container.querySelector('.activity-chip.live')?.textContent).toBe('0 s');
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(container.querySelector('.activity-chip.live')?.textContent).toBe('3 s');

    rerender(<ActivityRow tools={[{ ...running, status: 'complete', exitCode: 0, result: 'ok' }]} />);
    expect(container.querySelector('.activity-chip.live')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
  });

  it('puts a written label first and keeps the deterministic sentence as the tail and the title', () => {
    const { container } = render(<ActivityRow tools={[edit]} writtenLabel="Named each activity group" />);
    expect(container.querySelector('.activity-label')?.textContent).toBe('Named each activity group');
    expect(container.querySelector('.activity-tail')?.textContent).toBe('Edited ActivityRow.tsx');
    expect(summary(container)).toHaveAttribute('title', 'Edited ActivityRow.tsx');
  });

  it('draws its chrome with stroke icons, never emoji or text glyphs', () => {
    const stopped = call('ws_grep', { toolId: 'tu_grep', input: { pattern: 'TODO' }, status: 'stopped', result: '[Stopped by user]' });
    const { container } = render(<ActivityRow tools={[edit, read, stopped]} />);
    fireEvent.click(summary(container));
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    const chrome = [...container.querySelectorAll('.activity-head, .activity-call-line')].map((el) => el.textContent).join(' ');
    expect(chrome).not.toMatch(/[←-⇿⌀-⏿■-◿☀-➿]|\p{Extended_Pictographic}/u);
    expect(within(container.querySelector('.activity-call.status-stopped') as HTMLElement).getByText('not run')).toBeInTheDocument();
  });
});

describe('resolvePath', () => {
  it('shows a workspace path relative and opens it absolute', () => {
    expect(resolvePath('/repo/src/a.ts', '/repo')).toEqual({ shown: 'src/a.ts', absolute: '/repo/src/a.ts' });
    expect(resolvePath('./src/a.ts', '/repo/')).toEqual({ shown: 'src/a.ts', absolute: '/repo/src/a.ts' });
    expect(resolvePath('/tmp/out.html', '/repo')).toEqual({ shown: '/tmp/out.html', absolute: '/tmp/out.html' });
  });

  it('has nothing to open for a relative path without a workspace', () => {
    expect(resolvePath('src/a.ts', '')).toEqual({ shown: 'src/a.ts', absolute: null });
  });
});
