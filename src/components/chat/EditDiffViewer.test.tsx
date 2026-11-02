import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EditDiffViewer } from './EditDiffViewer';
import { useUIStore } from '@/stores/uiStore';
import { ApiError } from '@/types/api';
import { MAX_DIFF_CELLS } from '@/utils/computeLineDiff';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import type { EditRecord } from '@/api/workspace';
import { CHAT_INSERT_EVENT } from '@/utils/uiEvents';

/**
 * The diff dialog an activity row's diff pill opens: the Git panel's own
 * dialog, fed with the stored texts of that one edit. Only the network is
 * faked; the rows come from the real diff code.
 */

const getEdit = vi.fn();
const undoEdit = vi.fn();
vi.mock('@/api/workspace', () => ({
  getEdit: (id: string) => getEdit(id),
  undoEdit: (id: string) => undoEdit(id),
}));

const record = (r: Partial<EditRecord>): EditRecord => ({
  path: 'src/app.ts',
  full_path: '/repo/src/app.ts',
  original: 'one\ntwo\nthree\n',
  modified: 'one\nTWO\nthree\nfour\n',
  added: 2,
  removed: 1,
  approximate: false,
  created: false,
  at: '2026-10-18T10:00:00+00:00',
  stored: true,
  ...r,
});

function openViewer(wsPath = '/repo') {
  // The pill hands over its path joined to the connected workspace; the
  // record holds the absolute path the edit wrote.
  useUIStore.setState({ wsPath, editDiff: { toolUseId: 'tu_edit', path: `${wsPath}/src/app.ts` } });
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
  undoEdit.mockReset();
  useUIStore.setState({ toasts: [] });
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
    getEdit.mockResolvedValue(
      record({ path: 'src/new.ts', full_path: '/repo/src/new.ts', original: '', modified: 'a\nb\nc\n', added: 3, removed: 0, created: true }),
    );

    const { container } = openViewer();

    expect(await screen.findByText('Added')).toBeTruthy();
    expect(getEdit).toHaveBeenCalledWith('tu_edit');
    expect(codeCells(container, 'added')).toEqual(['a', 'b', 'c']);
    expect(codeCells(container, 'removed')).toEqual([]);
    // The header holds the whole path, its folders set apart from the name.
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/new.tsAdded');
    expect(container.querySelector('.git-diff-dir')?.textContent).toBe('/repo/src/');
    expect(container.querySelector('.git-diff-stats')?.textContent).toBe('+3 -0');
  });

  it('calls an existing empty file the write filled Modified, not Added', async () => {
    getEdit.mockResolvedValue(record({ path: 'pkg/__init__.py', original: '', modified: 'X = 1\n', added: 1, removed: 0 }));

    const { container } = openViewer();

    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(screen.queryByText('Added')).toBeNull();
    expect(codeCells(container, 'added')).toEqual(['X = 1']);
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
    // The pill's own path, absolute like a record's.
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/app.ts');

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(useUIStore.getState().editDiff).toBeNull();
  });

  it('keeps one path form from loading to loaded', async () => {
    let resolve: (r: EditRecord) => void = () => {};
    getEdit.mockReturnValue(new Promise<EditRecord>((r) => { resolve = r; }));

    const { container } = openViewer();

    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/app.ts');
    resolve(record({}));
    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/app.tsModified');
  });

  it('shows the path the edit wrote, not one joined to a workspace connected since', async () => {
    getEdit.mockResolvedValue(record({}));

    const { container } = openViewer('/elsewhere');

    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/app.tsModified');
  });

  it('shows the pill path for a record kept before it held the full path', async () => {
    getEdit.mockResolvedValue(record({ full_path: undefined }));

    const { container } = openViewer();

    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(container.querySelector('.git-diff-title')?.textContent).toBe('/repo/src/app.tsModified');
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

  it('offers no Undo for an edit without an undo copy', async () => {
    getEdit.mockResolvedValue(record({ undoable: false }));

    openViewer();

    expect(await screen.findByText('Modified')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Undo/ })).toBeNull();
  });

  it('Undo puts back this one write by its tool call and closes the dialog', async () => {
    getEdit.mockResolvedValue(record({ undoable: true }));
    undoEdit.mockResolvedValue({ path: 'src/app.ts', restored: true, removed: false });
    const inserted: string[] = [];
    const onInsert = (e: Event) => inserted.push((e as CustomEvent<{ text: string }>).detail.text);
    window.addEventListener(CHAT_INSERT_EVENT, onInsert);

    openViewer();
    fireEvent.click(await screen.findByRole('button', { name: /Undo/ }));

    await waitFor(() => expect(useUIStore.getState().editDiff).toBeNull());
    window.removeEventListener(CHAT_INSERT_EVENT, onInsert);
    expect(undoEdit).toHaveBeenCalledWith('tu_edit');
    expect(useUIStore.getState().toasts.some((t) => t.type === 'success')).toBe(true);
    expect(inserted).toHaveLength(1);
    expect(inserted[0]).toContain('src/app.ts');
  });

  it('a refused Undo says why and keeps the dialog open', async () => {
    getEdit.mockResolvedValue(record({ undoable: true }));
    undoEdit.mockRejectedValue(new ApiError(409, 'src/app.ts changed after this change was written.', '/api/workspace/undo', 'POST'));

    openViewer();
    fireEvent.click(await screen.findByRole('button', { name: /Undo/ }));

    await waitFor(() => expect(useUIStore.getState().toasts.some((t) => t.type === 'error')).toBe(true));
    const toast = useUIStore.getState().toasts.find((t) => t.type === 'error');
    expect(toast?.message).toContain('changed after');
    expect(useUIStore.getState().editDiff).not.toBeNull();
  });
});
