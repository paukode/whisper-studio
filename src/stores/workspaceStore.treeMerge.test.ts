import { describe, it, expect, beforeEach } from 'vitest';
import type { FileTreeEntry } from '@/types/workspace';
import { mergeTree, useWorkspaceStore } from './workspaceStore';

// `listDir` returns only a directory's immediate children, so the fresh root
// listing fed to a refresh has NO grandchildren. `mergeTree` merges that
// one-level listing into the existing (possibly deep, lazily-expanded) tree so
// expanded folders keep their loaded children instead of being clobbered empty.

function dir(path: string, children?: FileTreeEntry[]): FileTreeEntry {
  const name = path.split('/').pop() ?? path;
  return children
    ? { name, path, type: 'directory', children }
    : { name, path, type: 'directory' };
}

function file(path: string): FileTreeEntry {
  return { name: path.split('/').pop() ?? path, path, type: 'file' };
}

describe('mergeTree', () => {
  it('keeps a surviving directory’s lazily-loaded children when the fresh listing has none', () => {
    const old = [
      dir('src', [file('src/a.ts'), file('src/b.ts')]),
      file('README.md'),
    ];
    // Fresh root re-listing: `src` comes back with no children (one level).
    const fresh = [dir('src'), file('README.md')];

    const merged = mergeTree(old, fresh);

    const src = merged.find((n) => n.path === 'src');
    expect(src?.type).toBe('directory');
    expect(src?.children?.map((c) => c.path)).toEqual(['src/a.ts', 'src/b.ts']);
  });

  it('preserves the directory node identity for a surviving, unchanged dir', () => {
    const srcNode = dir('src', [file('src/a.ts')]);
    const merged = mergeTree([srcNode], [dir('src')]);
    // Same object reference => React keys / memoized subtrees stay stable.
    expect(merged[0]).toBe(srcNode);
  });

  it('drops entries that disappeared from the fresh listing', () => {
    const old = [dir('gone', [file('gone/x.ts')]), file('keep.ts')];
    const fresh = [file('keep.ts')];

    const merged = mergeTree(old, fresh);

    expect(merged.map((n) => n.path)).toEqual(['keep.ts']);
    expect(merged.some((n) => n.path === 'gone')).toBe(false);
  });

  it('adds a new file that only appears in the fresh listing', () => {
    const old = [file('a.ts')];
    const fresh = [file('a.ts'), file('b.ts')];

    const merged = mergeTree(old, fresh);

    expect(merged.map((n) => n.path)).toEqual(['a.ts', 'b.ts']);
  });

  it('adds a new directory without children (children are lazy-loaded on expand)', () => {
    const merged = mergeTree([], [dir('newdir')]);
    expect(merged[0].path).toBe('newdir');
    expect(merged[0].children).toBeUndefined();
  });

  it('keeps object identity for a file that stayed a file', () => {
    const keep = file('keep.ts');
    // Fresh listing carries a brand-new object with the same path.
    const merged = mergeTree([keep], [file('keep.ts')]);
    expect(merged[0]).toBe(keep);
  });

  it('takes the fresh node when a path changed type (dir -> file), dropping stale children', () => {
    const old = [dir('thing', [file('thing/inner.ts')])];
    const fresh = [file('thing')];

    const merged = mergeTree(old, fresh);

    expect(merged[0].type).toBe('file');
    expect(merged[0].children).toBeUndefined();
  });

  it('recurses when the fresh node itself carries a deeper listing', () => {
    const old = [
      dir('src', [
        dir('src/lib', [file('src/lib/a.ts')]),
        file('src/z.ts'),
        file('src/old.ts'),
      ]),
    ];
    // A deeper fresh listing of `src`: lib comes back childless, z stays,
    // old.ts is gone, new.ts appears.
    const fresh = [
      dir('src', [dir('src/lib'), file('src/z.ts'), file('src/new.ts')]),
    ];

    const merged = mergeTree(old, fresh);
    const src = merged.find((n) => n.path === 'src');
    const lib = src?.children?.find((c) => c.path === 'src/lib');

    // lib keeps its previously loaded grandchildren
    expect(lib?.children?.map((c) => c.path)).toEqual(['src/lib/a.ts']);
    // z.ts kept, new.ts added, old.ts dropped
    expect(src?.children?.map((c) => c.path).sort()).toEqual(
      ['src/lib', 'src/new.ts', 'src/z.ts'],
    );
  });
});

describe('workspaceStore.mergeFileTree', () => {
  beforeEach(() => {
    useWorkspaceStore.setState({ fileTree: [], editorTabs: [], activeTabPath: null });
  });

  it('merges a fresh listing into the store tree, preserving children and re-sorting', () => {
    // Seed a tree with an expanded folder carrying loaded children.
    useWorkspaceStore.getState().setFileTree([
      dir('src', [file('src/a.ts')]),
      file('README.md'),
    ]);

    // AI writes a new file at the root; refresh re-lists the root (one level).
    useWorkspaceStore.getState().mergeFileTree([
      dir('src'),
      file('README.md'),
      file('notes.md'),
    ]);

    const tree = useWorkspaceStore.getState().fileTree;
    // Directory sorts before files; files alphabetical case-insensitively
    // ('n' < 'r', so notes.md precedes README.md).
    expect(tree.map((n) => n.path)).toEqual(['src', 'notes.md', 'README.md']);
    const src = tree.find((n) => n.path === 'src');
    expect(src?.children?.map((c) => c.path)).toEqual(['src/a.ts']);
  });
});
