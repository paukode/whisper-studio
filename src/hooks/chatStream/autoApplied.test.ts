import { beforeEach, describe, expect, it, vi } from 'vitest';

const files = vi.hoisted(() => ({ queryFile: vi.fn() }));
vi.mock('@/api/workspace', () => files);

import { syncAutoAppliedTab } from './autoApplied';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useUIStore } from '@/stores/uiStore';
import { SSEEventDataSchema } from '@/types/schemas/chat.schema';

const WRITTEN = 'import os\nimport sys\n\n\ndef f( x ):\n    return  x+1\n';
const RUFFED = 'import sys\n\n\ndef f(x):\n    return x + 1\n';

function connect(root: string | undefined) {
  useUIStore.getState().setWsConnected(Boolean(root), root);
}

/** Open app.py in the connected workspace, as the file tree does. */
function openTab(content: string) {
  useWorkspaceStore.getState().openTab('app.py', content, 'python');
}

const tab = () => useWorkspaceStore.getState().editorTabs.find((t) => t.path === 'app.py');
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
  useWorkspaceStore.setState({ editorTabs: [], activeTabPath: null });
  useUIStore.setState({ toasts: [] });
  connect('/ws/A');
});

describe('syncAutoAppliedTab', () => {
  it('shows what ruff left on disk, not the content the model wrote', async () => {
    openTab('old\n');
    files.queryFile.mockResolvedValue({ path: 'app.py', content: RUFFED, size: RUFFED.length });
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    expect(tab()?.content).toBe(WRITTEN);
    await flush();
    expect(tab()?.content).toBe(RUFFED);
    // Still diffable against what the tab held before the write.
    expect(tab()?.originalContent).toBe('old\n');
    expect(tab()?.isDirty).toBe(true);
  });

  it('leaves a tab alone once the user has typed in it', async () => {
    openTab('old\n');
    let resolve: (v: unknown) => void = () => {};
    files.queryFile.mockReturnValue(new Promise((r) => (resolve = r)));
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    useWorkspaceStore.getState().markDirty('app.py', `${WRITTEN}# typed\n`);
    resolve({ path: 'app.py', content: RUFFED, size: RUFFED.length });
    await flush();
    expect(tab()?.content).toBe(`${WRITTEN}# typed\n`);
  });

  it('does nothing for a file that is not open', () => {
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    expect(files.queryFile).not.toHaveBeenCalled();
  });

  it('follows a second inline write of the same file', async () => {
    openTab('old\n');
    files.queryFile.mockResolvedValueOnce({ path: 'app.py', content: RUFFED, size: 1 });
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    await flush();
    // The tab is dirty from the first write, but holds nothing of the user's.
    files.queryFile.mockResolvedValueOnce({ path: 'app.py', content: 'second\n', size: 1 });
    syncAutoAppliedTab('app.py', 'second\n', '/ws/A');
    await flush();
    expect(tab()?.content).toBe('second\n');
    expect(useUIStore.getState().toasts).toHaveLength(0);
  });

  it("keeps the user's unsaved edits when the assistant writes the same file, and says so", async () => {
    openTab('old\n');
    useWorkspaceStore.getState().markDirty('app.py', 'my unsaved edit\n');
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    await flush();
    expect(tab()?.content).toBe('my unsaved edit\n');
    expect(files.queryFile).not.toHaveBeenCalled();
    const [toast] = useUIStore.getState().toasts;
    expect(toast.type).toBe('warning');
    expect(toast.message).toContain('The tab keeps your edits');
  });

  it("never lets a write in workspace B reach a tab kept from workspace A", async () => {
    // A: the user types in app.py, disconnects (the dirty tab is kept) and
    // connects B, where the assistant writes B's app.py inline.
    openTab('print("A")\n');
    useWorkspaceStore.getState().markDirty('app.py', 'print("A, my edit")\n');
    connect(undefined);
    useWorkspaceStore.getState().closeCleanTabs();
    connect('/ws/B');
    files.queryFile.mockResolvedValue({ path: 'app.py', content: 'print("B")\n', size: 11 });
    syncAutoAppliedTab('app.py', 'print("B")\n', '/ws/B');
    await flush();
    expect(tab()).toMatchObject({ root: '/ws/A', content: 'print("A, my edit")\n' });
    expect(files.queryFile).not.toHaveBeenCalled();

    // Back on A, saving the tab writes the user's own edit, never B's file.
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{}', { status: 200 })));
    vi.stubGlobal('fetch', fetchMock);
    connect('/ws/A');
    await useWorkspaceStore.getState().saveTab('app.py');
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      path: 'app.py',
      content: 'print("A, my edit")\n',
    });
    vi.unstubAllGlobals();
  });

  it('syncs by the root the frame names, not by what is connected when it arrives', async () => {
    openTab('old\n');
    connect('/ws/B'); // the user switched while the write was in flight
    syncAutoAppliedTab('app.py', WRITTEN, '/ws/A');
    expect(tab()?.content).toBe(WRITTEN);
  });

  it('keeps the frame root through the chat schema', () => {
    const parsed = SSEEventDataSchema.parse({
      ws_auto_applied: { path: 'app.py', content: 'x', workspace_root: '/ws/A' },
    });
    expect(parsed.ws_auto_applied?.workspace_root).toBe('/ws/A');
  });
});

describe('opening a file whose path a tab of another root holds', () => {
  it("replaces a clean tab left from the other root with this workspace's file", () => {
    openTab('A content\n');
    connect('/ws/B');
    useWorkspaceStore.getState().openTab('app.py', 'B content\n', 'python');
    expect(tab()).toMatchObject({ root: '/ws/B', content: 'B content\n', isDirty: false });
  });

  it('keeps a dirty tab of the other root and says why', () => {
    openTab('A content\n');
    useWorkspaceStore.getState().markDirty('app.py', 'A edit\n');
    connect('/ws/B');
    useWorkspaceStore.getState().openTab('app.py', 'B content\n', 'python');
    expect(tab()).toMatchObject({ root: '/ws/A', content: 'A edit\n' });
    expect(useUIStore.getState().toasts[0].message).toContain('unsaved edits from /ws/A');
  });
});
