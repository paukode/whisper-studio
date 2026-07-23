import React, { useCallback } from 'react';
import { DiffEditor, type DiffOnMount } from '@monaco-editor/react';
import { useTheme } from '@/providers/ThemeProvider';
import { getMonacoTheme, registerWhisperThemes } from './MonacoEditor';

export interface DiffViewerProps {
  /** Original file content (left side). */
  original: string;
  /** Modified file content (right side). */
  modified: string;
  /** Monaco language ID for syntax highlighting. */
  language?: string;
  /** Optional file path for display purposes. */
  filePath?: string;
}

/**
 * Read-only side-by-side Monaco diff. Used by the file-tree
 * "Select for Compare" → "Compare with Selected" flow (rendered as a
 * synthetic 'diff' editor tab). Reuses the editor's Whisper themes so the
 * comparison matches the rest of the workspace editor.
 */
export const DiffViewer: React.FC<DiffViewerProps> = ({
  original,
  modified,
  language = 'plaintext',
  filePath,
}) => {
  const { resolvedTheme } = useTheme();

  const handleMount: DiffOnMount = useCallback(
    (_editor, monaco) => {
      registerWhisperThemes(monaco);
      monaco.editor.setTheme(getMonacoTheme(resolvedTheme));
    },
    [resolvedTheme],
  );

  return (
    <div
      className="diff-viewer"
      role="region"
      aria-label={filePath ? `Diff: ${filePath}` : 'File diff'}
      style={{ height: '100%', minHeight: 0 }}
    >
      <DiffEditor
        original={original}
        modified={modified}
        language={language}
        theme={getMonacoTheme(resolvedTheme)}
        onMount={handleMount}
        options={{
          readOnly: true,
          renderSideBySide: true,
          minimap: { enabled: false },
          fontSize: 13,
          lineNumbers: 'on',
          scrollBeyondLastLine: false,
          automaticLayout: true,
          originalEditable: false,
        }}
      />
    </div>
  );
};
