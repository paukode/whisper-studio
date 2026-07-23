import { create } from 'zustand';
import type { FileTreeEntry, EditorTab } from '@/types/workspace';
import { dialogConfirm } from './uiStore';

export interface WorkspaceState {
  fileTree: FileTreeEntry[];
  editorTabs: EditorTab[];
  activeTabPath: string | null;
  // Actions
  setFileTree: (entries: FileTreeEntry[]) => void;
  /**
   * Merge a fresh (one-level) root listing into the existing tree, preserving
   * the lazily-loaded `children` of directories that still exist. Used by the
   * workspace-refresh flow so an AI write / approval / rename does not clobber
   * expanded folders (which would then render open-but-empty). See `mergeTree`.
   */
  mergeFileTree: (entries: FileTreeEntry[]) => void;
  openTab: (path: string, content: string, language: string, viewerType?: EditorTab['viewerType']) => void;
  openDiffTab: (leftPath: string, leftContent: string, rightPath: string, rightContent: string, language: string) => void;
  closeTab: (path: string) => void;
  /**
   * User-initiated close: if the tab has unsaved edits, prompt to discard
   * before closing; otherwise close immediately. Kept separate from the pure
   * `closeTab` action so programmatic mass-close (session switch, workspace
   * connect) can stay prompt-free — wiring the confirm into `closeTab` itself
   * would fire one prompt per tab during those loops.
   */
  confirmCloseTab: (path: string) => Promise<void>;
  setActiveTab: (path: string) => void;
  markDirty: (path: string, content: string) => void;
  saveTab: (path: string) => Promise<void>;
  refreshTabContent: (path: string, content: string) => void;
  setViewerType: (path: string, viewerType: EditorTab['viewerType']) => void;
  toggleDiffMode: (path: string) => void;
}

export const useWorkspaceStore = create<WorkspaceState>()((set, get) => ({
  fileTree: [],
  editorTabs: [],
  activeTabPath: null,

  setFileTree: (entries: FileTreeEntry[]) => {
    set({ fileTree: sortFileTree(entries) });
  },

  mergeFileTree: (entries: FileTreeEntry[]) => {
    set((state) => ({ fileTree: sortFileTree(mergeTree(state.fileTree, entries)) }));
  },

  openTab: (path: string, content: string, language: string, viewerType?: EditorTab['viewerType']) => {
    const { editorTabs } = get();
    const existing = editorTabs.find((tab) => tab.path === path);
    if (existing) {
      set({ activeTabPath: path });
      return;
    }
    const newTab: EditorTab = {
      path,
      language,
      content,
      originalContent: content,
      isDirty: false,
      viewerType,
    };
    set({
      editorTabs: [...editorTabs, newTab],
      activeTabPath: path,
    });
  },

  openDiffTab: (leftPath: string, leftContent: string, rightPath: string, rightContent: string, language: string) => {
    // Synthetic tab key: it is never a real file path, so every file
    // operation (read/save/reload) short-circuits on viewerType === 'diff'.
    const key = `${leftPath} ↔ ${rightPath}`;
    const { editorTabs } = get();
    if (editorTabs.some((tab) => tab.path === key)) {
      set({ activeTabPath: key });
      return;
    }
    const newTab: EditorTab = {
      path: key,
      language,
      content: leftContent,
      originalContent: leftContent,
      isDirty: false,
      viewerType: 'diff',
      comparePath: rightPath,
      compareContent: rightContent,
    };
    set({
      editorTabs: [...editorTabs, newTab],
      activeTabPath: key,
    });
  },

  closeTab: (path: string) => {
    const { editorTabs, activeTabPath } = get();
    const index = editorTabs.findIndex((tab) => tab.path === path);
    if (index === -1) return;

    const filtered = editorTabs.filter((tab) => tab.path !== path);
    let nextActiveTab = activeTabPath;

    if (activeTabPath === path) {
      if (filtered.length === 0) {
        nextActiveTab = null;
      } else if (index < filtered.length) {
        // Activate the tab that took the closed tab's position
        nextActiveTab = filtered[index].path;
      } else {
        // Closed the last tab — activate the new last tab
        nextActiveTab = filtered[filtered.length - 1].path;
      }
    }

    set({
      editorTabs: filtered,
      activeTabPath: nextActiveTab,
    });
  },

  confirmCloseTab: async (path: string) => {
    const { editorTabs, closeTab } = get();
    const tab = editorTabs.find((t) => t.path === path);
    if (tab?.isDirty) {
      const basename = path.split('/').pop() || path;
      const ok = await dialogConfirm({
        title: 'Discard unsaved changes?',
        message: `${basename} has unsaved changes. Close without saving?`,
        danger: true,
        confirmText: 'Discard',
      });
      if (ok !== true) return;
    }
    closeTab(path);
  },

  setActiveTab: (path: string) => {
    const { editorTabs } = get();
    const exists = editorTabs.some((tab) => tab.path === path);
    if (exists) {
      set({ activeTabPath: path });
    }
  },

  markDirty: (path: string, content: string) => {
    set((state) => ({
      editorTabs: state.editorTabs.map((tab) =>
        tab.path === path ? { ...tab, content, isDirty: true } : tab,
      ),
    }));
  },

  saveTab: async (path: string) => {
    const { editorTabs } = get();
    const tab = editorTabs.find((t) => t.path === path);
    if (!tab) return;

    const res = await fetch('/api/workspace/write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content: tab.content }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Save failed' }));
      throw new Error(err.error ?? 'Save failed');
    }

    set((state) => ({
      editorTabs: state.editorTabs.map((t) =>
        t.path === path
          ? { ...t, isDirty: false, originalContent: t.content }
          : t,
      ),
    }));
  },

  refreshTabContent: (path: string, content: string) => {
    set((state) => ({
      editorTabs: state.editorTabs.map((tab) =>
        tab.path === path
          ? { ...tab, content, originalContent: content, isDirty: false }
          : tab,
      ),
    }));
  },

  setViewerType: (path: string, viewerType: EditorTab['viewerType']) => {
    set((state) => ({
      editorTabs: state.editorTabs.map((tab) =>
        tab.path === path ? { ...tab, viewerType } : tab,
      ),
    }));
  },

  toggleDiffMode: (path: string) => {
    set((state) => ({
      editorTabs: state.editorTabs.map((tab) =>
        tab.path === path ? { ...tab, diffMode: !tab.diffMode } : tab,
      ),
    }));
  },
}));

/**
 * Merge a fresh directory listing into an existing tree, preserving the
 * lazily-loaded `children` of directories that still exist (matched by path).
 *
 * The workspace API's `listDir` returns only one level, so a fresh directory
 * node carries NO `children`. Replacing the tree with such a listing would drop
 * every subtree the user expanded, leaving those folders rendered open-but-empty
 * (their `expanded` flag lives locally in FileTreeNode, not in the tree). This
 * merge keeps each surviving directory's previously loaded subtree instead.
 *
 * Rules (pure, order-independent — the caller re-sorts):
 *  - directory that still exists: keep the previous node (its loaded children
 *    and object identity survive); if the fresh node itself carries children
 *    (a deeper listing), recurse into them.
 *  - file that still exists: keep the previous node (identity preserved).
 *  - path that changed type (file<->directory): take the fresh node verbatim,
 *    so no stale children carry over.
 *  - new path: added as-is (directories start without children, lazy-loaded).
 *  - path absent from the fresh listing: dropped (not mapped).
 */
export function mergeTree(
  oldNodes: FileTreeEntry[],
  freshNodes: FileTreeEntry[],
): FileTreeEntry[] {
  const oldByPath = new Map(oldNodes.map((n) => [n.path, n]));
  return freshNodes.map((fresh) => {
    const prev = oldByPath.get(fresh.path);
    if (!prev || prev.type !== fresh.type) return fresh;
    if (fresh.type === 'directory' && fresh.children) {
      // Fresh node carries a deeper listing — merge it into prev's children.
      return { ...prev, children: mergeTree(prev.children ?? [], fresh.children) };
    }
    // Directory with no fresh children (one-level listing) or unchanged file:
    // keep the previous node so its subtree and identity are preserved.
    return prev;
  });
}

/** Sort entries: directories first, then alphabetically by name (case-insensitive). Recursive. */
function sortFileTree(entries: FileTreeEntry[]): FileTreeEntry[] {
  return [...entries]
    .sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    })
    .map((entry) =>
      entry.children ? { ...entry, children: sortFileTree(entry.children) } : entry,
    );
}
