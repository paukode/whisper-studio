import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import Editor, { type OnMount, type OnChange } from '@monaco-editor/react';
import type { editor } from 'monaco-editor';
import { useTheme } from '@/providers/ThemeProvider';
import { useMonaco } from '@/hooks/useMonaco';
import { useLsp } from '@/hooks/useLsp';
import { useUIStore } from '@/stores/uiStore';
import { LspStatusIndicator } from './LspStatusIndicator';

/** Monaco namespace, as handed to onMount by @monaco-editor/react. */
type MonacoNamespace = typeof import('monaco-editor');

/**
 * Monaco view state (scroll, cursor, folds) keyed by file path, cached at
 * module scope so it survives the per-tab remount WorkspacePanel forces via
 * `key={activeTab.path}`. Each keyed instance is short-lived, so a component
 * ref can't hold this across the switch — it lives here, written on unmount and
 * read on the next mount for the same path (see useLayoutEffect + handleMount).
 */
const viewStateCache = new Map<string, editor.ICodeEditorViewState>();

/**
 * Editor colors per mode. They mirror the theme tokens and the --hl-*
 * syntax palette in static/modules/themes.css (Monaco needs literal hex).
 */
const MONACO_PALETTES = {
  dark: {
    base: 'vs-dark', bg: '#111111', fg: '#d4d4d4', lineNo: '#5a5a5a', lineNoActive: '#a8a8a8',
    accent: '#3dd68c', selection: '#3dd68c2e', lineHighlight: '#ffffff08', border: '#1e1e1e',
    keyword: 'c792ea', string: '3dd68c', number: 'ffcb6b', comment: '6a6a6a', fn: '82aaff',
    type: 'ffcb6b', variable: 'f07178', tag: 'f07178', attr: 'f78c6c',
  },
  light: {
    base: 'vs', bg: '#f3f3f1', fg: '#1a1a1a', lineNo: '#a3a3a0', lineNoActive: '#4f4f4f',
    accent: '#0f7a4a', selection: '#0f7a4a24', lineHighlight: '#00000006', border: '#e0e0dc',
    keyword: '7c3aed', string: '0f7a4a', number: 'b45309', comment: '8a8a8a', fn: '1d4ed8',
    type: 'b45309', variable: 'c2410c', tag: 'cf222e', attr: 'c2410c',
  },
} as const;

/** Define the Whisper editor themes once Monaco is loaded. */
let themesRegistered = false;
export function registerWhisperThemes(monaco: typeof import('monaco-editor')) {
  if (themesRegistered) return;
  themesRegistered = true;

  for (const [mode, p] of Object.entries(MONACO_PALETTES)) {
    monaco.editor.defineTheme(`whisper-${mode}`, {
      base: p.base,
      inherit: true,
      rules: [
        { token: 'comment', foreground: p.comment, fontStyle: 'italic' },
        { token: 'keyword', foreground: p.keyword },
        { token: 'string', foreground: p.string },
        { token: 'number', foreground: p.number },
        { token: 'type', foreground: p.type },
        { token: 'variable', foreground: p.variable },
        { token: 'tag', foreground: p.tag },
        { token: 'attribute.name', foreground: p.attr },
        { token: 'function', foreground: p.fn },
      ],
      colors: {
        'editor.background': p.bg,
        'editor.foreground': p.fg,
        'editorLineNumber.foreground': p.lineNo,
        'editorLineNumber.activeForeground': p.lineNoActive,
        'editorCursor.foreground': p.accent,
        'editor.selectionBackground': p.selection,
        'editor.lineHighlightBackground': p.lineHighlight,
        'editorWidget.border': p.border,
        'editorIndentGuide.background1': p.border,
        'focusBorder': p.accent,
      },
    });
  }
}

/** Map the resolved mode to its Whisper Monaco theme ID. */
export function getMonacoTheme(resolvedTheme: 'light' | 'dark'): string {
  return resolvedTheme === 'light' ? 'whisper-light' : 'whisper-dark';
}

export interface MonacoEditorProps {
  filePath: string;
  content: string;
  language: string;
  onContentChange?: (content: string) => void;
  onSave?: (path: string, content: string) => void;
  onClose?: (path: string) => void;
  onCursorChange?: (line: number, col: number) => void;
  readOnly?: boolean;
  /** Cited line range (1-based) to reveal + highlight — from a #wsfile citation. */
  revealRange?: { start: number; end: number };
  /** Monotonic counter: bumping it re-reveals the range (e.g. re-clicking a
   *  citation for an already-open file), even when revealRange is unchanged. */
  revealRev?: number;
  /** For a workspace tab: the root its file belongs to (EditorTab.root; null
   *  for a tab opened with none connected). The language server runs only
   *  while that root is the connected one, so a tab kept from another
   *  workspace is never analysed as this one's file. Omitted for an editor
   *  that is not a workspace tab. */
  workspaceRoot?: string | null;
}

/**
 * Wraps @monaco-editor/react with Whisper Studio theme integration,
 * matching the original vanilla JS editor config exactly:
 *   DM Mono 13px, minimap off, wordWrap off, tabSize 4,
 *   bracket colorization, smooth cursor, custom themes, Ctrl+S/W keybindings,
 *   cursor position tracking, and live theme switching.
 */
export const MonacoEditor: React.FC<MonacoEditorProps> = ({
  filePath,
  content,
  language,
  onContentChange,
  onSave,
  onClose,
  onCursorChange,
  readOnly = false,
  revealRange,
  revealRev,
  workspaceRoot,
}) => {
  const { resolvedTheme } = useTheme();
  const { setEditorTheme } = useMonaco();
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof import('monaco-editor') | null>(null);
  const decorationsRef = useRef<editor.IEditorDecorationsCollection | null>(null);

  // The LSP client needs the live editor + Monaco namespace, which only exist
  // after mount. Hold them in state so the useLsp effect re-runs once ready.
  const [lspTarget, setLspTarget] = useState<{
    editor: editor.IStandaloneCodeEditor;
    monaco: MonacoNamespace;
  } | null>(null);

  const wsConnected = useUIStore((s) => s.wsConnected);
  const wsPath = useUIStore((s) => s.wsPath);

  // Live language client tunneling to server/lsp_proxy.py. No-ops for
  // unsupported languages, when no workspace is connected, and for a tab of
  // another root than the connected one.
  const { status: lspStatus, active: lspActive, failure: lspFailure } = useLsp({
    monaco: lspTarget?.monaco ?? null,
    editorInstance: lspTarget?.editor ?? null,
    language,
    filePath,
    workspacePath: wsPath,
    enabled: wsConnected && !readOnly && (workspaceRoot === undefined || workspaceRoot === wsPath),
  });

  // Reveal + highlight a cited line range (clamped to the model). Used both on
  // mount (after view-state restore, so the citation wins) and whenever revealRev
  // bumps from a re-click. Keyed on the primitive start/end (not the revealRange
  // object) so a fresh-but-identical object on every parent render does NOT re-fire
  // and snap the editor back / reset the cursor mid-edit.
  const revealStart = revealRange?.start;
  const revealEnd = revealRange?.end;
  const revealCited = useCallback(() => {
    const ed = editorRef.current;
    const monaco = monacoRef.current;
    const model = ed?.getModel();
    if (!ed || !monaco || !model || revealStart == null) return;
    const last = model.getLineCount();
    const start = Math.min(Math.max(revealStart, 1), last);
    const end = Math.min(Math.max(revealEnd ?? revealStart, start), last);
    ed.revealLineInCenter(start, monaco.editor.ScrollType.Smooth);
    ed.setPosition({ lineNumber: start, column: 1 });
    decorationsRef.current?.clear();
    decorationsRef.current = ed.createDecorationsCollection([
      {
        range: new monaco.Range(start, 1, end, model.getLineMaxColumn(end)),
        options: {
          isWholeLine: true,
          className: 'cited-line-highlight',
          linesDecorationsClassName: 'cited-line-gutter',
        },
      },
    ]);
  }, [revealStart, revealEnd]);

  const monacoTheme = getMonacoTheme(resolvedTheme);

  // Persist Monaco view state (scroll + cursor + folds) across the per-tab
  // remount. WorkspacePanel keys this editor by path (`key={activeTab.path}`),
  // so a tab switch fully unmounts this component and the child <Editor>
  // disposes its model — discarding the built-in per-model view state. We save
  // into the module-level cache on unmount and restore it in handleMount for
  // the same path.
  //
  // This MUST be a *layout* effect: the child <Editor> disposes its model in a
  // passive-effect cleanup, and passive cleanups run child-before-parent on
  // unmount — so a plain useEffect cleanup here would fire after disposal and
  // saveViewState() would return null. Layout-effect cleanups fire in the
  // earlier commit phase, while the editor is still alive.
  useLayoutEffect(() => {
    return () => {
      const vs = editorRef.current?.saveViewState();
      if (vs) viewStateCache.set(filePath, vs);
    };
  }, [filePath]);

  // Live theme switching. Goes through useMonaco so the same path is used
  // anywhere we ever need to switch the editor theme outside this component.
  useEffect(() => {
    const m = monacoRef.current;
    if (!m) return;
    setEditorTheme(getMonacoTheme(resolvedTheme));
  }, [resolvedTheme, setEditorTheme]);

  const handleMount: OnMount = useCallback(
    (ed, monaco) => {
      editorRef.current = ed;
      monacoRef.current = monaco;

      // Hand the editor + Monaco namespace to the LSP client effect.
      setLspTarget({ editor: ed, monaco });

      // Register custom themes on first mount
      registerWhisperThemes(monaco);
      monaco.editor.setTheme(getMonacoTheme(resolvedTheme));

      // Ctrl+S — Save file
      ed.addAction({
        id: 'whisper-save',
        label: 'Save File',
        keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
        run: () => {
          const currentContent = ed.getValue();
          onSave?.(filePath, currentContent);
        },
      });

      // Ctrl+W — Close tab
      ed.addAction({
        id: 'whisper-close-tab',
        label: 'Close Tab',
        keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyW],
        run: () => {
          onClose?.(filePath);
        },
      });

      // Cursor position tracking → status bar
      ed.onDidChangeCursorPosition((e) => {
        onCursorChange?.(e.position.lineNumber, e.position.column);
      });

      // Restore cached view state (scroll + cursor) for this path, if any.
      // Runs before revealCited() so an explicit citation still wins.
      const savedViewState = viewStateCache.get(filePath);
      if (savedViewState) {
        ed.restoreViewState(savedViewState);
      }

      // A cited line range wins over restored scroll position.
      revealCited();
      if (!revealRange) ed.focus();
    },
    [filePath, onSave, onClose, onCursorChange, resolvedTheme, revealCited, revealRange],
  );

  // Re-reveal when the citation target changes or is re-clicked (revealRev bump).
  useEffect(() => {
    revealCited();
  }, [revealRev, revealCited]);

  const handleChange: OnChange = useCallback(
    (value) => {
      if (value !== undefined) {
        onContentChange?.(value);
      }
    },
    [onContentChange],
  );

  return (
    <div className="monaco-editor-wrapper" role="region" aria-label={`Editor: ${filePath}`} style={{ position: 'relative', width: '100%', height: '100%' }}>
      {lspActive && (
        <LspStatusIndicator status={lspStatus} language={language} failure={lspFailure} />
      )}
      <Editor
        height="100%"
        path={filePath}
        value={content}
        language={language}
        theme={monacoTheme}
        onChange={handleChange}
        onMount={handleMount}
        options={{
          readOnly,
          fontSize: 13,
          fontFamily: "'DM Mono', ui-monospace, Menlo, monospace",
          lineNumbers: 'on',
          minimap: { enabled: false },
          wordWrap: 'off',
          tabSize: 4,
          insertSpaces: true,
          scrollBeyondLastLine: false,
          automaticLayout: true,
          renderWhitespace: 'selection',
          bracketPairColorization: { enabled: true },
          guides: { bracketPairs: true, indentation: true },
          smoothScrolling: true,
          cursorBlinking: 'smooth',
          cursorSmoothCaretAnimation: 'on',
          padding: { top: 8, bottom: 8 },
        }}
      />
    </div>
  );
};
