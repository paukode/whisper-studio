export interface FileTreeEntry {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: FileTreeEntry[];
}

export interface EditorTab {
  path: string;
  language: string;
  content: string;
  originalContent: string;
  isDirty: boolean;
  diffMode?: boolean;
  cursorPosition?: { lineNumber: number; column: number };
  scrollPosition?: { scrollTop: number; scrollLeft: number };
  viewState?: unknown;
  /** Non-text files use a dedicated viewer instead of Monaco.
   *  'diff' is a synthetic side-by-side comparison tab (see comparePath/
   *  compareContent); its `path` is a synthetic key, not a real file. */
  viewerType?: 'image' | 'pdf' | 'spreadsheet' | 'word' | 'markdown' | 'notebook' | 'csv' | 'binary' | 'diff';
  /** For 'diff' tabs: the right-hand (modified) file's real path. */
  comparePath?: string;
  /** For 'diff' tabs: the right-hand (modified) file's content. */
  compareContent?: string;
  /** The workspace root the tab was opened in. `path` is relative to it, so
   *  a save while another root (or none) is connected is refused rather than
   *  writing this content into the other workspace's file of the same name.
   *  A tab of another root is a different file: an inline write, a refresh,
   *  an open and the language server of the connected root leave it alone. */
  root?: string;
  /** What the last inline write left on disk and showed in this tab
   *  (syncAutoAppliedTab). A dirty tab still holding exactly this carries no
   *  edits of the user's. */
  appliedContent?: string;
}
