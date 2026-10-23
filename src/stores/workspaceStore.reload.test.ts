import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FileTreeEntry } from '@/types/workspace';

vi.mock('@/api/workspace', () => ({ listDir: vi.fn() }));

import { listDir } from '@/api/workspace';
import { eventPath, loadedDirs, useWorkspaceStore } from './workspaceStore';

/**
 * The tree follows the disk through `reloadDirs`: the backend's
 * workspace_changed event names the folders whose listing may differ, and
 * only the loaded ones among them are listed again. A request naming none
 * (the header button) re-lists the root and every loaded folder. This pins
 * the field case: a folder expanded once kept its first listing forever.
 */
const dir = (path: string, children?: FileTreeEntry[]): FileTreeEntry => ({
  name: path.split('/').pop()!,
  path,
  type: 'directory',
  ...(children ? { children } : {}),
});
const file = (path: string): FileTreeEntry => ({ name: path.split('/').pop()!, path, type: 'file' });

const names = (nodes: FileTreeEntry[] | undefined) => (nodes ?? []).map((n) => n.name);
const listed = () => vi.mocked(listDir).mock.calls.map((c) => c[0]);

beforeEach(() => {
  vi.mocked(listDir).mockReset();
  useWorkspaceStore.setState({
    fileTree: [
      dir('capacity-planner', [
        dir('capacity-planner/backend', [file('capacity-planner/backend/pyproject.toml')]),
        dir('capacity-planner/docs'),
        file('capacity-planner/SPEC.md'),
      ]),
      dir('emea-delivery-app'),
      file('planner.xlsx'),
    ],
  });
});

describe('workspaceStore.reloadDirs', () => {
  it('re-lists only the loaded folders the event names, and merges what comes back', async () => {
    vi.mocked(listDir).mockImplementation(async (path) =>
      path === 'capacity-planner'
        ? [
            dir('capacity-planner/backend'),
            dir('capacity-planner/docs'),
            dir('capacity-planner/frontend'),
            file('capacity-planner/README.md'),
          ]
        : [],
    );

    await useWorkspaceStore.getState().reloadDirs([
      'capacity-planner',
      'capacity-planner/docs', // never expanded: nothing to refresh
      'emea-delivery-app', // same
      'somewhere/else', // not in the tree at all
    ]);

    expect(listed()).toEqual(['capacity-planner']);
    const planner = useWorkspaceStore.getState().fileTree[0];
    expect(names(planner.children)).toEqual(['backend', 'docs', 'frontend', 'README.md']);
    // The expanded subfolder kept the children it had loaded.
    expect(names(planner.children?.find((n) => n.name === 'backend')?.children)).toEqual(['pyproject.toml']);
  });

  it('a request naming no folders re-lists the root and every loaded folder', async () => {
    vi.mocked(listDir).mockResolvedValue([]);

    await useWorkspaceStore.getState().reloadDirs(null);

    expect(listed()).toEqual(['.', 'capacity-planner', 'capacity-planner/backend']);
  });

  it('the root is named as the empty path', async () => {
    vi.mocked(listDir).mockResolvedValue([dir('capacity-planner'), dir('new-app'), file('planner.xlsx')]);

    await useWorkspaceStore.getState().reloadDirs(['']);

    expect(listed()).toEqual(['.']);
    const tree = useWorkspaceStore.getState().fileTree;
    expect(names(tree)).toEqual(['capacity-planner', 'new-app', 'planner.xlsx']);
    // A folder that vanished went with its subtree; a surviving one kept its children.
    expect(names(tree[0].children)).toEqual(['backend', 'docs', 'SPEC.md']);
  });

  it('a listing that arrives after a newer one of the same folder is dropped', async () => {
    let resolveFirst!: (v: FileTreeEntry[]) => void;
    vi.mocked(listDir)
      .mockImplementationOnce(() => new Promise<FileTreeEntry[]>((r) => { resolveFirst = r; }))
      .mockResolvedValueOnce([file('capacity-planner/current.md')]);

    const first = useWorkspaceStore.getState().reloadDirs(['capacity-planner']);
    const second = useWorkspaceStore.getState().reloadDirs(['capacity-planner']);
    await second;
    resolveFirst([file('capacity-planner/stale.md')]);
    await first;

    expect(names(useWorkspaceStore.getState().fileTree[0].children)).toEqual(['current.md']);
  });

  it('a failed listing leaves the tree as it was', async () => {
    vi.mocked(listDir).mockRejectedValue(new Error('404'));

    await useWorkspaceStore.getState().reloadDirs(['capacity-planner']);

    expect(names(useWorkspaceStore.getState().fileTree[0].children)).toEqual(['backend', 'docs', 'SPEC.md']);
  });
});

describe('reloadDirs with the listing as the backend returns it', () => {
  // Listing '.' gives "./"-prefixed paths; the watcher names folders without
  // the prefix. The field case: only root-level changes ever showed.
  it('a folder the event names without "./" is re-listed by its tree path', async () => {
    useWorkspaceStore.setState({
      fileTree: [
        dir('./capacity-planner-staffing-macos', [
          dir('./capacity-planner-staffing-macos/dist', [file('./capacity-planner-staffing-macos/dist/app.zip')]),
          dir('./capacity-planner-staffing-macos/docs'),
        ]),
      ],
    });
    vi.mocked(listDir).mockImplementation(async (path) =>
      path === './capacity-planner-staffing-macos/dist'
        ? [file('./capacity-planner-staffing-macos/dist/app.zip'), file('./capacity-planner-staffing-macos/dist/new.txt')]
        : [],
    );

    await useWorkspaceStore.getState().reloadDirs(['capacity-planner-staffing-macos/dist']);

    expect(listed()).toEqual(['./capacity-planner-staffing-macos/dist']);
    const dist = useWorkspaceStore.getState().fileTree[0].children?.find((n) => n.name === 'dist');
    expect(names(dist?.children)).toEqual(['app.zip', 'new.txt']);
  });

  it('eventPath maps tree paths onto the names the event uses', () => {
    expect(eventPath('.')).toBe('');
    expect(eventPath('./docs/a.md')).toBe('docs/a.md');
    expect(eventPath('docs/a.md')).toBe('docs/a.md');
    expect(eventPath('./.venv')).toBe('.venv');
  });
});

describe('loadedDirs', () => {
  it('lists the folders that carry children, parents first', () => {
    expect(loadedDirs(useWorkspaceStore.getState().fileTree)).toEqual([
      'capacity-planner',
      'capacity-planner/backend',
    ]);
  });
});
