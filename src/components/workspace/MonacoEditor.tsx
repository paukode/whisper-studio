import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import Editor, { type OnMount, type OnChange } from '@monaco-editor/react';
import type { editor } from 'monaco-editor';
import { useTheme } from '@/providers/ThemeProvider';
import { useMonaco } from '@/hooks/useMonaco';
import { useLsp, type LspStatus } from '@/hooks/useLsp';
import { useUIStore } from '@/stores/uiStore';

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

/** Define custom Whisper themes once Monaco is loaded. */
let themesRegistered = false;
export function registerWhisperThemes(monaco: typeof import('monaco-editor')) {
  if (themesRegistered) return;
  themesRegistered = true;

  monaco.editor.defineTheme('whisper-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#111113',
      'editor.foreground': '#e4e2de',
      'editorLineNumber.foreground': '#5e5d5a',
      'editorCursor.foreground': '#e2a336',
      'editor.selectionBackground': '#e2a33633',
      'editor.lineHighlightBackground': '#ffffff08',
    },
  });

  monaco.editor.defineTheme('whisper-light', {
    base: 'vs',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#f5f3ef',
      'editor.foreground': '#1a1918',
      'editorLineNumber.foreground': '#a09d99',
      'editorCursor.foreground': '#c4841d',
      'editor.selectionBackground': '#c4841d22',
      'editor.lineHighlightBackground': '#00000008',
    },
  });

  monaco.editor.defineTheme('taw-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#1f1e1d',
      'editor.foreground': '#f5f4ee',
      'editorLineNumber.foreground': '#78756e',
      'editorCursor.foreground': '#d97757',
      'editor.selectionBackground': '#d9775733',
      'editor.lineHighlightBackground': '#ffffff08',
    },
  });

  monaco.editor.defineTheme('taw-light', {
    base: 'vs',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#faf9f5',
      'editor.foreground': '#1f1e1d',
      'editorLineNumber.foreground': '#a3a097',
      'editorCursor.foreground': '#cf6a48',
      'editor.selectionBackground': '#d9775722',
      'editor.lineHighlightBackground': '#00000006',
    },
  });
}

/** Map resolved theme names to custom Whisper Monaco theme IDs. */
export function getMonacoTheme(resolvedTheme: string): string {
  switch (resolvedTheme) {
    case 'light':
    case 'light-daltonized':
      return 'whisper-light';
    case 'dark':
    case 'dark-daltonized':
      return 'whisper-dark';
    case 'light-taw':
      return 'taw-light';
    case 'dark-taw':
      return 'taw-dark';
    case 'light-high-contrast':
      return 'hc-light';
    case 'dark-high-contrast':
      return 'hc-black';
    default:
      return 'whisper-dark';
  }
}

/** Human-readable label for the LSP status dot's tooltip + text. */
function lspStatusLabel(status: LspStatus, language: string): string {
  const server = `${language} language server`;
  switch (status) {
    case 'connecting':
      return `Connecting to ${server}…`;
    case 'connected':
      return `${server}: connected`;
    case 'error':
      return `${server}: unavailable`;
    default:
      return `${server}: off`;
  }
}

/**
 * Small language-server status pill rendered in the editor's top-right chrome.
 * Uses the shared `.ws-lsp-status` / `.ws-lsp-dot` classes from
 * static/modules/lsp-client.css so the dot color tracks the connection state.
 */
const LspStatusIndicator: React.FC<{ status: LspStatus; language: string }> = ({
  status,
  language,
}) => {
  const dotClass = status === 'closed' ? '' : ` ${status}`;
  const label = lspStatusLabel(status, language);
  return (
    <div
      className="ws-lsp-status"
      title={label}
      role="status"
      aria-label={label}
      style={{ position: 'absolute', top: 4, right: 14, zIndex: 4 }}
    >
      <span className={`ws-lsp-dot${dotClass}`} />
      <span>LSP</span>
    </div>
  );
};

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
}

/**
 * Wraps @monaco-editor/react with Whisper Studio theme integration,
 * matching the original vanilla JS editor config exactly:
 *   IBM Plex Mono 13px, minimap off, wordWrap off, tabSize 4,
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
  // unsupported languages or when no workspace is connected.
  const { status: lspStatus, active: lspActive } = useLsp({
    monaco: lspTarget?.monaco ?? null,
    editorInstance: lspTarget?.editor ?? null,
    language,
    filePath,
    workspacePath: wsPath,
    enabled: wsConnected && !readOnly,
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
      {lspActive && <LspStatusIndicator status={lspStatus} language={language} />}
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
          fontFamily: 'IBM Plex Mono, monospace',
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
