import React, { useCallback, useRef, useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useUIStore } from '@/stores/uiStore';
import { queryFile, type BinaryFileInfo } from '@/api/workspace';
import { getLangForPath } from '@/utils/languageDetection';
import { FileTree } from './FileTree';
import { EditorTabs } from './EditorTabs';
import { MonacoEditor } from './MonacoEditor';
import { Breadcrumb } from './Breadcrumb';
import { StatusBar } from './StatusBar';
import { ImageViewer } from '@/components/viewers/ImageViewer';
import { PDFViewer } from '@/components/viewers/PDFViewer';
import { MarkdownPreview } from '@/components/viewers/MarkdownPreview';
import { CSVViewer } from '@/components/viewers/CSVViewer';
import { SpreadsheetViewer } from '@/components/viewers/SpreadsheetViewer';
import { WordViewer } from '@/components/viewers/WordViewer';
import { NotebookViewer } from '@/components/viewers/NotebookViewer';
import { DiffViewer } from './DiffViewer';
import { WorkspaceContextMenu, type ContextMenuState } from './WorkspaceContextMenu';
import { GitChangesPanel } from '@/components/git/GitChangesPanel';
import { toError } from '@/utils/toError';

/** Extensions handled as CSV/TSV table view */
const CSV_EXTS = new Set(['.csv', '.tsv']);

/** Extensions handled as notebook */
const NOTEBOOK_EXTS = new Set(['.ipynb']);

/** Extensions handled as markdown preview */
const MARKDOWN_EXTS = new Set(['.md', '.mdx']);

function getExtension(path: string): string {
  const name = (path.split('/').pop() ?? '').toLowerCase();
  const dotIdx = name.lastIndexOf('.');
  return dotIdx === -1 ? '' : name.slice(dotIdx);
}

export interface WorkspacePanelProps {
  onCollapse?: () => void;
}

/**
 * Full workspace panel matching the original ws-ide-layout structure.
 * Routes files to appropriate viewers based on backend binary detection
 * and extension-based detection for CSV/TSV, markdown, and notebooks.
 */
export const WorkspacePanel: React.FC<WorkspacePanelProps> = ({ onCollapse }) => {
  const editorTabs = useWorkspaceStore((s) => s.editorTabs);
  const activeTabPath = useWorkspaceStore((s) => s.activeTabPath);
  const openTab = useWorkspaceStore((s) => s.openTab);
  const confirmCloseTab = useWorkspaceStore((s) => s.confirmCloseTab);
  const markDirty = useWorkspaceStore((s) => s.markDirty);
  const saveTab = useWorkspaceStore((s) => s.saveTab);
  const setViewerType = useWorkspaceStore((s) => s.setViewerType);

  const [cursorLine, setCursorLine] = useState(1);
  const [cursorCol, setCursorCol] = useState(1);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');

  // Tree pane resize state
  const treePaneRef = useRef<HTMLDivElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const isDraggingRef = useRef(false);

  const activeTab = editorTabs.find((t) => t.path === activeTabPath) ?? null;
  const hasEditor = editorTabs.length > 0;

  // Reload non-dirty open tabs when workspace files change (AI writes, approvals, etc.)
  useEffect(() => {
    const handler = () => {
      const ws = useWorkspaceStore.getState();
      for (const tab of ws.editorTabs) {
        if (tab.isDirty) continue; // Don't overwrite user edits
        if (tab.viewerType === 'image' || tab.viewerType === 'pdf' || tab.viewerType === 'binary' || tab.viewerType === 'diff') continue;
        queryFile(tab.path)
          .then((data) => {
            if ('content' in data && typeof data.content === 'string') {
              // Only update if content actually changed
              const current = useWorkspaceStore.getState().editorTabs.find(t => t.path === tab.path);
              if (current && !current.isDirty && current.originalContent !== data.content) {
                useWorkspaceStore.getState().refreshTabContent(tab.path, data.content);
              }
            }
          })
          .catch(() => { /* file may have been deleted */ });
      }
    };
    window.addEventListener('whisper-workspace-refresh', handler);
    return () => window.removeEventListener('whisper-workspace-refresh', handler);
  }, []);

  // When the last editor tab closes, the IDE layout drops back to tree-only.
  // The tree/editor resize drag (handleResizeMouseDown) sets an imperative
  // inline `flex: 0 0 Npx` on the tree pane. React doesn't track that style,
  // so it would otherwise persist after the editor pane unmounts — pinning
  // the tree to its dragged width and leaving an empty gap where the editor
  // used to be. Clear it so the CSS `.ws-tree-pane { flex: 1 }` takes over
  // and the tree fills the panel again.
  useEffect(() => {
    if (!hasEditor && treePaneRef.current) {
      treePaneRef.current.style.flex = '';
    }
  }, [hasEditor]);

  // Debounce the raw search input by 300ms. setDebouncedQuery runs in the timer
  // callback (asynchronous), so this is not a setState-in-effect violation.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  // File search via react-query, keyed on the debounced query. Disabled (and so
  // not run) while the query is empty.
  const { data: searchData } = useQuery({
    queryKey: ['file-search', debouncedQuery],
    queryFn: async (): Promise<Array<{ path: string }>> => {
      const r = await fetch(`/api/workspace/search-files?q=${encodeURIComponent(debouncedQuery)}&limit=100`);
      if (!r.ok) return [];
      const data = await r.json();
      return data?.results ?? data?.files ?? [];
    },
    enabled: debouncedQuery.length > 0,
  });

  // null = no active search (show the tree); array = results (possibly empty).
  // While a search is pending (data still undefined) keep showing the tree.
  const searchResults: Array<{ path: string }> | null = debouncedQuery ? (searchData ?? null) : null;

  /**
   * File open handler matching the original openFileViewer flow:
   * 1. Query backend for file metadata (binary detection)
   * 2. Route to appropriate viewer based on type
   */
  const handleFileSelect = useCallback(
    async (path: string) => {
      // Check if already open
      const existing = useWorkspaceStore.getState().editorTabs.find((t) => t.path === path);
      if (existing) {
        useWorkspaceStore.getState().setActiveTab(path);
        return;
      }

      try {
        const data = await queryFile(path);

        // Binary file — route by type
        if ('binary' in data && data.binary) {
          const info = data as BinaryFileInfo;
          switch (info.type) {
            case 'image':
              openTab(path, '', 'plaintext', 'image');
              return;
            case 'pdf':
              openTab(path, '', 'plaintext', 'pdf');
              return;
            case 'spreadsheet':
              openTab(path, '', 'plaintext', 'spreadsheet');
              return;
            case 'word':
              openTab(path, '', 'plaintext', 'word');
              return;
            default:
              // Generic binary — show info message
              openTab(path, `(Binary file, ${info.size} bytes)`, 'plaintext', 'binary');
              return;
          }
        }

        // Text file — check for special viewers
        const content = 'content' in data ? (data.content as string) : '';
        const ext = getExtension(path);
        const language = getLangForPath(path);

        if (CSV_EXTS.has(ext)) {
          openTab(path, content, language, 'csv');
          return;
        }

        if (NOTEBOOK_EXTS.has(ext)) {
          openTab(path, content, language, 'notebook');
          return;
        }

        if (MARKDOWN_EXTS.has(ext)) {
          openTab(path, content, language, 'markdown');
          return;
        }

        // Default: open in Monaco editor
        openTab(path, content, language);
      } catch {
        useUIStore.getState().addToast({ type: 'error', message: `Failed to open ${path.split('/').pop()}` });
      }
    },
    [openTab],
  );

  const handleContentChange = useCallback(
    (content: string) => {
      if (activeTabPath) {
        markDirty(activeTabPath, content);
      }
    },
    [activeTabPath, markDirty],
  );

  const handleSave = useCallback(
    async (path: string) => {
      try {
        await saveTab(path);
        useUIStore.getState().addToast({ type: 'success', message: 'File saved', duration: 2000 });
      } catch (err) {
        useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
      }
    },
    [saveTab],
  );

  // Close handler passed to Monaco as onClose — fires from the editor's
  // Cmd/Ctrl+W keybinding. Prompts to discard first when the tab is dirty.
  const handleClose = useCallback(
    (path: string) => {
      void confirmCloseTab(path);
    },
    [confirmCloseTab],
  );

  const handleCursorChange = useCallback((line: number, col: number) => {
    setCursorLine(line);
    setCursorCol(col);
  }, []);

  /** Switch from a special viewer to Monaco editor (used by CSV Edit Raw and Markdown Edit). */
  const handleSwitchToEditor = useCallback(
    (path: string) => {
      setViewerType(path, undefined);
    },
    [setViewerType],
  );

  /** Switch from Monaco back to Markdown preview. */
  const handleSwitchToMarkdown = useCallback(
    (path: string) => {
      setViewerType(path, 'markdown');
    },
    [setViewerType],
  );

  /** Disconnect workspace — matching vanilla wsDisconnectBtn behavior. */
  const handleDisconnect = useCallback(async () => {
    try {
      await fetch('/api/workspace/disconnect', { method: 'POST' });
    } catch { /* ignore */ }
    useUIStore.getState().setWsConnected(false);
  }, []);

  /** Context menu handler for file tree. */
  const handleContextMenu = useCallback(
    (event: React.MouseEvent, path: string, type: 'file' | 'directory') => {
      event.preventDefault();
      event.stopPropagation();
      setContextMenu({ x: event.clientX, y: event.clientY, path, type });
    },
    [],
  );

  const handleCloseContextMenu = useCallback(() => {
    setContextMenu(null);
  }, []);

  /** Tree/editor resize handle — mousedown starts drag. */
  const handleResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRef.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const handleMouseMove = (ev: MouseEvent) => {
      if (!isDraggingRef.current || !layoutRef.current || !treePaneRef.current) return;
      const rect = layoutRef.current.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const clamped = Math.max(100, Math.min(rect.width - 150, x));
      treePaneRef.current.style.flex = `0 0 ${clamped}px`;
    };

    const handleMouseUp = () => {
      isDraggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, []);

  const renderViewer = () => {
    if (!activeTab) return null;
    const vt = activeTab.viewerType;

    if (vt === 'image') return <ImageViewer key={activeTab.path} filePath={activeTab.path} />;
    if (vt === 'pdf') return <PDFViewer key={activeTab.path} filePath={activeTab.path} />;
    if (vt === 'spreadsheet') return <SpreadsheetViewer key={activeTab.path} filePath={activeTab.path} />;
    if (vt === 'word') return <WordViewer key={activeTab.path} filePath={activeTab.path} onEdit={() => handleSwitchToEditor(activeTab.path)} />;

    if (vt === 'diff') {
      return (
        <DiffViewer
          key={activeTab.path}
          original={activeTab.content}
          modified={activeTab.compareContent ?? ''}
          language={activeTab.language}
          filePath={activeTab.path}
        />
      );
    }

    if (vt === 'markdown') {
      return (
        <MarkdownPreview
          key={`${activeTab.path}-preview`}
          filePath={activeTab.path}
          content={activeTab.content}
          onEdit={() => handleSwitchToEditor(activeTab.path)}
        />
      );
    }

    if (vt === 'csv') {
      return (
        <CSVViewer
          key={activeTab.path}
          filePath={activeTab.path}
          content={activeTab.content}
          onEditRaw={() => handleSwitchToEditor(activeTab.path)}
        />
      );
    }

    if (vt === 'notebook') {
      return <NotebookViewer filePath={activeTab.path} content={activeTab.content} />;
    }

    if (vt === 'binary') {
      return (
        <div className="ws-editor-empty" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
          <p>{activeTab.content}</p>
        </div>
      );
    }

    // Check if this was previously a markdown file in editor mode — show back-to-preview button
    const ext = getExtension(activeTab.path);
    const isMarkdownFile = MARKDOWN_EXTS.has(ext);
    const isCsvFile = CSV_EXTS.has(ext);

    // Default: Monaco editor
    return (
      <>
        {(isMarkdownFile || isCsvFile) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
            {isMarkdownFile && (
              <button className="btn btn-sm" onClick={() => handleSwitchToMarkdown(activeTab.path)} type="button">
                Preview
              </button>
            )}
            {isCsvFile && (
              <button className="btn btn-sm" onClick={() => setViewerType(activeTab.path, 'csv')} type="button">
                Table View
              </button>
            )}
          </div>
        )}
        <MonacoEditor
          key={activeTab.path}
          filePath={activeTab.path}
          content={activeTab.content}
          language={activeTab.language}
          onContentChange={handleContentChange}
          onSave={handleSave}
          onClose={handleClose}
          onCursorChange={handleCursorChange}
        />
      </>
    );
  };

  const isTextEditor = activeTab && !activeTab.viewerType;

  return (
    <>
      {/* Header */}
      <div className="panel-header">
        <h2>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          Workspace
        </h2>
        <div className="panel-header-actions">
          <button
            className="btn btn-sm"
            id="wsRefreshBtn"
            // Manual override: forces FileTree (and any other listener — git
            // changes panel, etc.) to re-list the workspace. Useful when the
            // user has changed files outside the app or just wants to
            // double-check that the tree reflects the latest disk state.
            onClick={() => window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'))}
            title="Refresh file tree"
            type="button"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10"/>
              <polyline points="1 20 1 14 7 14"/>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/>
              <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14"/>
            </svg>
          </button>
          <button
            className="btn btn-sm"
            id="wsDisconnectBtn"
            onClick={() => void handleDisconnect()}
            title="Disconnect workspace"
            type="button"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
          {onCollapse && (
            <button
              className="btn btn-sm ws-collapse-btn"
              id="wsCollapseBtn"
              onClick={onCollapse}
              title="Collapse workspace"
              type="button"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="15 18 9 12 15 6"/>
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* IDE Layout: tree-pane | resize-handle | editor-pane */}
      <div ref={layoutRef} className={`ws-ide-layout${hasEditor ? ' has-editor' : ''}`}>
        {/* Tree pane */}
        <div ref={treePaneRef} className="ws-tree-pane">
          <div className="ws-search">
            <input
              type="text"
              className="ws-search-input"
              placeholder="Search files..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
          <div className="panel-body ws-file-tree">
            {searchResults !== null ? (
              searchResults.length === 0 ? (
                <div className="ws-tree-file" style={{ color: 'var(--text-muted)', cursor: 'default' }}>No results</div>
              ) : (
                <div role="listbox" aria-label="Search results">
                  {searchResults.map((f) => (
                    <div
                      key={f.path}
                      role="option"
                      tabIndex={0}
                      className="ws-tree-file ws-search-result"
                      title={f.path}
                      onClick={() => { handleFileSelect(f.path); setSearchQuery(''); setDebouncedQuery(''); }}
                      onKeyDown={(e) => { if (e.key === 'Enter') { handleFileSelect(f.path); setSearchQuery(''); setDebouncedQuery(''); } }}
                    >
                      {f.path}
                    </div>
                  ))}
                </div>
              )
            ) : (
              <FileTree onFileSelect={handleFileSelect} onContextMenu={handleContextMenu} />
            )}
          </div>

          {/* Git changes panel — below file tree, matching vanilla ws-git-changes */}
          <GitChangesPanel onFileOpen={handleFileSelect} />
        </div>

        {/* Tree/editor resize handle — functional drag */}
        {hasEditor && (
          <div
            className="ws-tree-resize-handle"
            onMouseDown={handleResizeMouseDown}
          >
            <div className="resize-handle-bar" />
          </div>
        )}

        {/* Editor pane */}
        {hasEditor && (
          <div className="ws-editor-pane">
            <EditorTabs />
            <Breadcrumb path={activeTabPath} />

            <div className="ws-editor-area" id="wsEditorArea">
              {renderViewer()}
            </div>

            {isTextEditor && (
              <StatusBar
                filePath={activeTabPath}
                line={cursorLine}
                col={cursorCol}
              />
            )}
          </div>
        )}
      </div>

      {/* Context menu overlay */}
      {contextMenu && (
        <WorkspaceContextMenu
          state={contextMenu}
          onClose={handleCloseContextMenu}
          onFileSelect={handleFileSelect}
        />
      )}
    </>
  );
};
