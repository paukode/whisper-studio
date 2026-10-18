import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EditDiffViewer } from './EditDiffViewer';
import { useUIStore } from '@/stores/uiStore';
import { ApiError } from '@/types/api';
import { MAX_DIFF_CELLS } from '@/utils/computeLineDiff';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import type { EditRecord } from '@/api/workspace';

/**
 * The diff dialog an activity row's diff pill opens: the Git panel's own
 * dialog, fed with the stored texts of that one edit. Only the network is
 * faked; the rows come from the real diff code.
 */

const getEdit = vi.fn();
vi.mock('@/api/workspace', () => ({
  getEdit: (id: string) => getEdit(id),
}));

const record = (r: Partial<EditRecord>): EditRecord => ({
  path: 'src/app.ts',
  original: 'one\ntwo\nthree\n',
  modified: 'one\nTWO\nthree\nfour\n',
  added: 2,
  removed: 1,
  approximate: false,
  at: '2026-10-18T10:00:00+00:00',
  stored: true,
  ...r,
});

function openViewer() {
  // The pill hands over the absolute path; the record holds the
  // workspace-relative one.
  useUIStore.setState({ wsPath: '/repo', editDiff: { toolUseId: 'tu_edit', path: '/repo/src/app.ts' } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EditDiffViewer />
    </QueryClientProvider>,
  );
}

const codeCells = (container: HTMLElement, kind: 'added' | 'removed') =>
  Array.from(container.querySelectorAll(`.git-diff-code.${kind}`)).map((el) => el.textContent?.slice(1));

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  useUIStore.setState({ editDiff: null, wsPath: '' });
  getEdit.mockReset();
});

describe('EditDiffViewer', () => {
  it('renders nothing while no diff pill asked for one', () => {
    useUIStore.setState({ editDiff: null });
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <EditDiffViewer />
      </QueryClientProvider>,
    );
    expect(container.innerHTML).toBe('');
    expect(getEdit).not.toHaveBeenCalled();
  });

  it('opens a created file against an empty original: every row an addition, status Added', async () => {
    getEdit.mockResolvedValue(record({ path: 'src/new.ts', original: '', modified: 'a\nb\nc\n', added: 3, removed: 0 }));

    const { container } = openViewer();

    expect(await screen.findByText('Added')).toBeTruthy();
    expect(getEdit).toHaveBeenCalledWith('tu_edit');
    expect(codeCells(container, 'added')).toEqual(['a', 'b', 'c']);
    expect(codeCells(container, 'removed')).toEqual([]);
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('new.tsAdded');
    expect(container.querySelector('.git-diff-path')?.textContent).toBe('src/new.ts');
    expect(container.querySelector('.git-diff-stats')?.textContent).toBe('+3 -0');
  });

  it('shows a modified file with its counts and both panes', async () => {
    getEdit.mockResolvedValue(record({}));

    const { container } = openViewer();

    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(container.querySelector('.git-diff-stats')?.textContent).toBe('+2 -1');
    expect(container.querySelector('.git-diff-grid.split')).toBeTruthy();
    // The replaced line sits opposite its replacement.
    expect(codeCells(container, 'removed')).toEqual(['two']);
    expect(codeCells(container, 'added')).toEqual(['TWO', 'four']);
    expect(container.querySelector('.git-diff-num.left')).toBeTruthy();
    expect(container.querySelector('.git-diff-num.right')).toBeTruthy();
    expect(screen.queryByText('approximate')).toBeNull();
  });

  it('marks estimated counts as approximate', async () => {
    getEdit.mockResolvedValue(record({ approximate: true }));

    openViewer();

    expect(await screen.findByText('approximate')).toBeTruthy();
  });

  it('says the diff was not kept when the server has no record, and still closes', async () => {
    getEdit.mockRejectedValue(
      new ApiError(404, 'No edit is recorded for that tool call', '/api/workspace/edits/tu_edit', 'GET'),
    );

    const { container } = openViewer();

    expect(await screen.findByText('The diff for this edit was not kept.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('app.ts');
    // The pill's absolute path, shown relative to the workspace like a record's.
    expect(container.querySelector('.git-diff-path')?.textContent).toBe('src/app.ts');

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(useUIStore.getState().editDiff).toBeNull();
  });

  it('keeps one path form from loading to loaded', async () => {
    let resolve: (r: EditRecord) => void = () => {};
    getEdit.mockReturnValue(new Promise<EditRecord>((r) => { resolve = r; }));

    const { container } = openViewer();

    expect(container.querySelector('.git-diff-path')?.textContent).toBe('src/app.ts');
    resolve(record({}));
    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(container.querySelector('.git-diff-path')?.textContent).toBe('src/app.ts');
  });

  it('closes on Esc', async () => {
    getEdit.mockResolvedValue(record({}));

    const { container } = openViewer();
    await screen.findByText('Modified');

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(useUIStore.getState().editDiff).toBeNull();
    expect(container.querySelector('.git-diff-dialog')).toBeNull();
  });

  it('says the diff was not kept for a record without texts, and keeps its counts', async () => {
    getEdit.mockResolvedValue(record({ path: '.env', original: null, modified: null, stored: false }));

    const { container } = openViewer();

    expect(await screen.findByText('The diff for this edit was not kept.')).toBeTruthy();
    expect(container.querySelector('.git-diff-stats')?.textContent).toBe('+2 -1');
    expect(container.querySelector('.git-diff-status')).toBeNull();
    expect(container.querySelector('.git-diff-grid')).toBeNull();
  });

  it('shows the too-large note when the texts cannot be aligned in the browser', async () => {
    // Both sides differ throughout, so the alignment table is over its cap.
    const n = Math.ceil(Math.sqrt(MAX_DIFF_CELLS)) + 10;
    const side = (p: string) => Array.from({ length: n }, (_, i) => `${p}${i}`).join('\n');
    getEdit.mockResolvedValue(record({ original: side('l'), modified: side('r'), added: n, removed: n }));

    const { container } = openViewer();

    expect(await screen.findByText('This file is too large to diff in the browser.')).toBeTruthy();
    expect(container.querySelector('.git-diff-grid')).toBeNull();
  });

  it('offers a retry for a failure that is not a missing record', async () => {
    getEdit.mockRejectedValue(new ApiError(0, "Can't reach the server.", '/api/workspace/edits/tu_edit', 'GET'));

    openViewer();

    expect(await screen.findByText("Can't reach the server.")).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('switches to the unified layout and keeps the choice', async () => {
    getEdit.mockResolvedValue(record({}));

    const { container } = openViewer();
    await screen.findByText('Modified');

    fireEvent.click(screen.getByRole('button', { name: 'Unified' }));

    expect(container.querySelector('.git-diff-grid.unified')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Unified' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'Split' }).getAttribute('aria-pressed')).toBe('false');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.GIT_DIFF_LAYOUT) ?? 'null')).toBe('unified');
  });
});
