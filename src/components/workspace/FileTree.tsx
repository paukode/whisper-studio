import React, { useCallback, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { FileTreeEntry } from '@/types/workspace';
import { listDir, renameFile } from '@/api/workspace';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useUIStore } from '@/stores/uiStore';
import { FileTreeNode } from './FileTreeNode';
import { InlineInput } from './InlineInput';
import { toError } from '@/utils/toError';

export interface FileTreeProps {
  /** Root path to load the tree from. Defaults to '.'. */
  rootPath?: string;
  /** Called when a file is selected in the tree. */
  onFileSelect: (path: string) => void;
  /** Called on right-click for context menu. */
  onContextMenu?: (event: React.MouseEvent, path: string, type: 'file' | 'directory') => void;
}

/**
 * Lazy-loading directory tree. Fetches children on expand via the
 * workspace API. Supports inline rename and new-file/new-folder inputs.
 */
export const FileTree: React.FC<FileTreeProps> = ({
  rootPath = '.',
  onFileSelect,
  onContextMenu,
}) => {
  const fileTree = useWorkspaceStore((s) => s.fileTree);
  const setFileTree = useWorkspaceStore((s) => s.setFileTree);
  const mergeFileTree = useWorkspaceStore((s) => s.mergeFileTree);
  const activeTabPath = useWorkspaceStore((s) => s.activeTabPath);
  const wsPath = useUIStore((s) => s.wsPath);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [newItemParent, setNewItemParent] = useState<string | null>(null);
  const [newItemType] = useState<'file' | 'directory'>('file');

  // Sync selected path when the active editor tab changes. During-render
  // previous-value pattern instead of setState-in-effect.
  const [prevActiveTabPath, setPrevActiveTabPath] = useState(activeTabPath);
  if (activeTabPath !== prevActiveTabPath) {
    setPrevActiveTabPath(activeTabPath);
    if (activeTabPath) setSelectedPath(activeTabPath);
  }

  const handleSelect = useCallback((path: string) => {
    setSelectedPath(path);
  }, []);

  // Load the root directory via react-query. A new queryKey on rootPath/wsPath
  // change (workspace switch) triggers a refetch.
  const { data: rootEntries, isLoading, isError: rootError } = useQuery({
    queryKey: ['file-tree-root', rootPath, wsPath],
    queryFn: () => listDir(rootPath),
  });

  // Mirror the fetched root listing into the workspace store. setFileTree is a
  // zustand action (not React setState), so this is not a setState-in-effect
  // violation. On error (API unavailable / no workspace), show the empty state.
  useEffect(() => {
    if (rootEntries) setFileTree(sortEntries(rootEntries));
    else if (rootError) setFileTree([]);
  }, [rootEntries, rootError, setFileTree]);

  // Listen for workspace-refresh events from SSE handler (file writes,
  // approvals, manual refresh button, etc.).
  //
  // Refreshes are debounced (~120 ms trailing) so the burst of events that
  // happens when the AI writes several files in quick succession collapses
  // into ONE listDir, and we tag each fetch with a monotonic sequence number
  // so an older in-flight response can never overwrite a newer one. Without
  // this, three rapid SSE events fired three concurrent listDir calls whose
  // resolution order was undefined — the slowest "winner" sometimes
  // contained only the first file, leaving the tree stale.
  //
  // The fresh listing is MERGED (not replaced) into the current tree via
  // `mergeFileTree`: `listDir` returns only the root's immediate children, so a
  // plain replace would drop every subtree the user had expanded — leaving
  // those folders rendered open-but-empty (expansion state lives locally in
  // FileTreeNode, not in the tree). Merging preserves loaded children of dirs
  // that still exist while adding new entries and dropping removed ones.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let seq = 0;
    let latestApplied = 0;

    const fetchNow = () => {
      const mySeq = ++seq;
      listDir(rootPath)
        .then((entries) => {
          if (mySeq < latestApplied) return; // a newer fetch already won
          latestApplied = mySeq;
          mergeFileTree(entries);
        })
        .catch(() => { /* ignore */ });
    };

    const handler = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fetchNow();
      }, 120);
    };
    window.addEventListener('whisper-workspace-refresh', handler);
    return () => {
      window.removeEventListener('whisper-workspace-refresh', handler);
      if (timer) clearTimeout(timer);
    };
  }, [rootPath, mergeFileTree]);

  // Listen for rename events from context menu — show inline rename input in the tree
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { path: string } | undefined;
      if (detail?.path) setRenamingPath(detail.path);
    };
    window.addEventListener('whisper-rename-file', handler);
    return () => window.removeEventListener('whisper-rename-file', handler);
  }, []);

  /**
   * Expand a directory by fetching its children from the API and
   * merging them into the tree.
   */
  const handleExpand = useCallback(
    async (dirPath: string) => {
      const children = await listDir(dirPath);
      setFileTree(
        mergeChildren(fileTree, dirPath, children),
      );
    },
    [fileTree, setFileTree],
  );

  const handleContextMenu = useCallback(
    (event: React.MouseEvent, path: string, type: 'file' | 'directory') => {
      onContextMenu?.(event, path, type);
    },
    [onContextMenu],
  );

  const handleRenameStart = useCallback((path: string) => {
    setRenamingPath(path);
  }, []);

  const handleRenameConfirm = useCallback(
    async (oldPath: string, newName: string) => {
      setRenamingPath(null);
      if (!newName.trim()) return;
      const oldName = oldPath.split('/').pop() ?? '';
      if (newName.trim() === oldName) return;
      // Backend renames within the same directory and expects a basename
      // (it rejects path separators), so pass the new name as-is.
      try {
        await renameFile(oldPath, newName.trim());
        // Merge, not replace: `listDir('.')` is one level, so replacing would
        // drop the children of every expanded folder (see mergeFileTree).
        const rootEntries = await listDir('.');
        mergeFileTree(rootEntries);
        useUIStore.getState().addToast({ type: 'success', message: `Renamed to ${newName.trim()}`, duration: 2000 });
      } catch (err) {
        useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
      }
    },
    [mergeFileTree],
  );

  const handleRenameCancel = useCallback(() => {
    setRenamingPath(null);
  }, []);

  const handleNewItemConfirm = useCallback(
    (_name: string) => {
      // New item creation is handled by the parent WorkspacePanel via API call
      setNewItemParent(null);
    },
    [],
  );

  const handleNewItemCancel = useCallback(() => {
    setNewItemParent(null);
  }, []);

  if (isLoading && fileTree.length === 0) {
    return (
      <div className="file-tree file-tree-loading" role="tree" aria-label="File tree" aria-busy="true">
        <span className="skeleton skeleton-text" style={{ width: '70%' }} />
        <span className="skeleton skeleton-text" style={{ width: '55%' }} />
        <span className="skeleton skeleton-text" style={{ width: '62%' }} />
      </div>
    );
  }

  return (
    <div className="file-tree" role="tree" aria-label="File tree">
      {fileTree.map((entry) => (
        <FileTreeNode
          key={entry.path}
          entry={entry}
          depth={0}
          selectedPath={selectedPath}
          onSelect={handleSelect}
          onFileSelect={onFileSelect}
          onExpand={handleExpand}
          onContextMenu={handleContextMenu}
          renamingPath={renamingPath}
          onRenameStart={handleRenameStart}
          onRenameConfirm={handleRenameConfirm}
          onRenameCancel={handleRenameCancel}
        />
      ))}

      {newItemParent != null && (
        <div className="file-tree-new-item" style={{ paddingLeft: '20px' }}>
          <InlineInput
            placeholder={newItemType === 'file' ? 'New file name…' : 'New folder name…'}
            onConfirm={handleNewItemConfirm}
            onCancel={handleNewItemCancel}
          />
        </div>
      )}

      {fileTree.length === 0 && !isLoading && (
        <div className="file-tree-empty">
          <span className="file-tree-empty-text">No files found</span>
        </div>
      )}
    </div>
  );
};

/**
 * Sort entries: directories first, then alphabetical by name (case-insensitive).
 */
function sortEntries(entries: FileTreeEntry[]): FileTreeEntry[] {
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
  });
}

/**
 * Recursively merge fetched children into the tree at the given directory path.
 * Children are sorted: directories first, then alphabetically.
 */
function mergeChildren(
  tree: FileTreeEntry[],
  dirPath: string,
  children: FileTreeEntry[],
): FileTreeEntry[] {
  const sorted = sortEntries(children);
  return tree.map((entry) => {
    if (entry.path === dirPath) {
      return { ...entry, children: sorted };
    }
    if (entry.children) {
      return { ...entry, children: mergeChildren(entry.children, dirPath, children) };
    }
    return entry;
  });
}
