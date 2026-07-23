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
}
