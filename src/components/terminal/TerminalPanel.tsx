import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { TerminalSession } from '@/types/terminal';
import { createTerminalSession, deleteTerminalSession } from '@/api/terminal';
import { getGitBranch, getGitWorktrees, addGitWorktree, removeGitWorktree, getWorktreeSession } from '@/api/git';
import { useUIStore } from '@/stores/uiStore';
import { useSessionStore } from '@/stores/sessionStore';
import { TerminalTab, type TerminalTabHandle } from './TerminalTab';
import { measureCellGrid } from '@/hooks/useTerminal';

let nextLabel = 1;

// Minimum height the chat/transcript row above the terminal must always keep,
// so its composer + header stay visible no matter how tall the terminal is
// dragged. Kept in sync with the `minHeight` floor on the chat row in
// AppShell.tsx — the terminal drag is clamped against it here, and the flex
// layout enforces it as a hard floor if the window is later resized smaller.
const MIN_CHAT_PX = 220;

interface SessionWithDims extends TerminalSession {
  cols: number;
  rows: number;
}

export const TerminalPanel: React.FC = () => {
  const [sessions, setSessions] = useState<SessionWithDims[]>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  // Set when POST /api/terminal/create fails, so the panel shows why it is
  // empty instead of a silent blank pane. Cleared on the next success.
  const [createError, setCreateError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);
  const [panelHeight, setPanelHeight] = useState(300);
  // Bumped on drag end; the effect below refits after the height commits.
  const [dragGen, setDragGen] = useState(0);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const [worktreeOpen, setWorktreeOpen] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newBranchName, setNewBranchName] = useState('');
  const [createBranch, setCreateBranch] = useState(false);
  const [busy, setBusy] = useState(false);

  const isDraggingRef = useRef(false);
  const startYRef = useRef(0);
  const startHeightRef = useRef(0);
  // Height of the panel's flex column parent (.panels-right-col), captured at
  // drag start so the terminal can't be dragged past what leaves the chat row
  // its MIN_CHAT_PX floor. Read once per drag — the container height is stable
  // while dragging.
  const dragMaxHeightRef = useRef(700);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const creatingRef = useRef(false);
  const sessionsRef = useRef(sessions);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const tabHandles = useRef<Map<string, TerminalTabHandle>>(new Map());
  const worktreeMenuRef = useRef<HTMLDivElement | null>(null);

  const queryClient = useQueryClient();

  // Branch + worktree state loads via react-query, gated on the panel being
  // expanded (`enabled: !collapsed`) and re-keyed on the current session (the
  // worktree-session lookup depends on it). The SSE/refresh listeners and user
  // actions call refreshGit() — which invalidates this key — to refetch.
  const { data: gitData } = useQuery({
    queryKey: ['terminal-git', currentSessionId],
    queryFn: async () => {
      const [b, w, ws] = await Promise.all([
        getGitBranch().catch(() => null),
        getGitWorktrees().catch(() => null),
        currentSessionId
          ? getWorktreeSession(currentSessionId).catch(() => null)
          : Promise.resolve(null),
      ]);
      return {
        branch: b?.branch ?? null,
        worktrees: w?.worktrees ?? [],
        activeWorktreeSession: ws?.session ?? null,
      };
    },
    enabled: !collapsed,
  });
  const branch = gitData?.branch ?? null;
  const worktrees = gitData?.worktrees ?? [];
  const activeWorktreeSession = gitData?.activeWorktreeSession ?? null;

  /** Refetch branch + worktree state by invalidating the query. */
  const refreshGit = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['terminal-git', currentSessionId] }),
    [queryClient, currentSessionId],
  );

  // Listen for the same SSE git-changed signal the GitChangesPanel uses
  // — when branch/HEAD/worktree state changes externally, refetch.
  useEffect(() => {
    if (collapsed) return;
    const es = new EventSource('/api/git/events');
    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as { type?: string };
        if (parsed.type === 'git-changed') void refreshGit();
      } catch { /* ignore malformed event */ }
    };
    return () => es.close();
  }, [collapsed, refreshGit]);

  // When an approval lands an enter/exit_worktree, the SSE watcher won't
  // fire (the worktree session lives in-memory, not in .git files). The
  // approval flow dispatches a workspace-refresh — piggyback on it so the
  // session badge updates without a second event channel.
  useEffect(() => {
    const handler = () => void refreshGit();
    window.addEventListener('whisper-workspace-refresh', handler);
    return () => window.removeEventListener('whisper-workspace-refresh', handler);
  }, [refreshGit]);

  /** Create a terminal session sized to the actual visible body. */
  const createNewSession = useCallback(async () => {
    if (creatingRef.current) return undefined;
    creatingRef.current = true;
    try {
      // measureCellGrid probes the rendered font; the null fallback matches
      // the server defaults and only errs small, which the post-open fit
      // corrects by growing an empty buffer (safe).
      const { cols, rows } = measureCellGrid(bodyRef.current);
      const wsPath = useUIStore.getState().wsPath;
      const data = await createTerminalSession(wsPath || '', cols, rows);
      const label = `Terminal ${nextLabel++}`;
      const session: SessionWithDims = {
        id: data.session_id,
        label,
        cwd: data.cwd,
        isConnected: true,
        cols,
        rows,
      };
      setSessions((prev) => [...prev, session]);
      setActiveTabId(session.id);
      setCreateError(null);
      return session;
    } catch (err) {
      console.warn('Failed to create terminal session:', err);
      setCreateError(err instanceof Error ? err.message : String(err));
      return undefined;
    } finally {
      creatingRef.current = false;
    }
  }, []);

  const handleNewTab = useCallback(async () => {
    const session = await createNewSession();
    if (session && collapsed) {
      setCollapsed(false);
    }
  }, [createNewSession, collapsed]);

  const handleCloseTab = useCallback(
    (id: string) => {
      void deleteTerminalSession(id).catch(() => {});
      tabHandles.current.delete(id);
      setSessions((prev) => {
        const updated = prev.filter((s) => s.id !== id);
        if (id === activeTabId && updated.length > 0) {
          setActiveTabId(updated[updated.length - 1].id);
        }
        if (updated.length === 0) {
          setActiveTabId(null);
          setCollapsed(true);
        }
        return updated;
      });
    },
    [activeTabId],
  );

  const handleSwitchTab = useCallback((id: string) => {
    setActiveTabId(id);
  }, []);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => !prev);
  }, []);

  // Create the first session only after the expanded body has rendered,
  // so measureCellGrid sees real dimensions. (Creating it inside the
  // toggle handler measured a null bodyRef and fell back to a 100x30
  // grid inside a ~15-row panel — the out-of-frame terminal bug.)
  useEffect(() => {
    if (collapsed || sessions.length > 0) return;
    void createNewSession();
  }, [collapsed, sessions.length, createNewSession]);

  const activeTabIdRef = useRef(activeTabId);
  useEffect(() => { activeTabIdRef.current = activeTabId; }, [activeTabId]);

  /**
   * One deliberate refit of the active tab. Shared by the header button,
   * drag end, and window-resize settle — never called per-tick, so the
   * shell answers a single SIGWINCH per user gesture.
   */
  const refitActive = useCallback(() => {
    const id = activeTabIdRef.current;
    if (id) tabHandles.current.get(id)?.refit();
  }, []);

  // Ctrl+` keyboard shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === '`') {
        e.preventDefault();
        toggleCollapsed();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [toggleCollapsed]);

  // Vertical resize
  const handleResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRef.current = true;
    startYRef.current = e.clientY;
    // Seed from the RENDERED height, not panelHeight state: with flexShrink:1
    // the flex layout can render the panel shorter than the state value, and
    // seeding from the stale state left a dead zone where dragging up did
    // nothing until the delta caught up to the difference.
    startHeightRef.current =
      panelRef.current?.getBoundingClientRect().height ?? panelHeight;
    // Cap the terminal at container height minus the chat's minimum, so
    // dragging up can never squeeze the composer out of view.
    const containerH = panelRef.current?.parentElement?.clientHeight ?? Infinity;
    dragMaxHeightRef.current = Math.max(120, containerH - MIN_CHAT_PX);
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
  }, [panelHeight]);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      const delta = startYRef.current - e.clientY;
      const cap = Math.min(700, dragMaxHeightRef.current);
      const newHeight = Math.max(120, Math.min(cap, startHeightRef.current + delta));
      setPanelHeight(newHeight);
    };
    const handleMouseUp = () => {
      if (!isDraggingRef.current) return;
      isDraggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Refit via the dragGen effect (not directly): the last mousemove's
      // setPanelHeight commits asynchronously, and refit measures the DOM.
      setDragGen((g) => g + 1);
    };
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [refitActive]);

  // One deliberate refit per completed drag, after the final panel height
  // has committed to the DOM (effects run post-commit).
  useEffect(() => {
    if (dragGen === 0) return;
    refitActive();
  }, [dragGen, refitActive]);

  // Refit after the window stops resizing (trailing debounce so the
  // shell answers one final SIGWINCH, not a storm of intermediate ones).
  useEffect(() => {
    let timer: number | undefined;
    const onResize = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(refitActive, 200);
    };
    window.addEventListener('resize', onResize);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('resize', onResize);
    };
  }, [refitActive]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      sessionsRef.current.forEach((s) => {
        void deleteTerminalSession(s.id).catch(() => {});
      });
    };
  }, []);

  // Close worktree menu on outside click
  useEffect(() => {
    if (!worktreeOpen) return;
    const handler = (e: MouseEvent) => {
      if (!worktreeMenuRef.current?.contains(e.target as Node)) {
        setWorktreeOpen(false);
        setShowAddForm(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [worktreeOpen]);

  const handleAddWorktree = useCallback(async () => {
    const name = newBranchName.trim();
    if (!name || busy) return;
    setBusy(true);
    try {
      await addGitWorktree(name, { createBranch });
      await refreshGit();
      setNewBranchName('');
      setCreateBranch(false);
      setShowAddForm(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to create worktree';
      useUIStore.getState().addToast({ type: 'error', message: msg, duration: 4000 });
    } finally {
      setBusy(false);
    }
  }, [newBranchName, createBranch, busy, refreshGit]);

  const handleRemoveWorktree = useCallback(async (path: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await removeGitWorktree(path, false);
      await refreshGit();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to remove worktree';
      useUIStore.getState().addToast({ type: 'error', message: msg, duration: 4000 });
    } finally {
      setBusy(false);
    }
  }, [busy, refreshGit]);

  const setTabHandle = useCallback((id: string) => (handle: TerminalTabHandle | null) => {
    if (handle) tabHandles.current.set(id, handle);
    else tabHandles.current.delete(id);
  }, []);

  return (
    <>
      {/* Toggle bar — visible when collapsed */}
      {collapsed && (
        <div className="ws-terminal-toggle-bar" onClick={toggleCollapsed} style={{ flexShrink: 0 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <polyline points="4 17 10 11 4 5"/>
            <line x1="12" y1="19" x2="20" y2="19"/>
          </svg>
          Terminal
          {branch && <span className="ws-terminal-branch-chip">⎇ {branch}</span>}
          {activeWorktreeSession && (
            <span className="ws-terminal-worktree-chip" title={`In worktree session: ${activeWorktreeSession.worktree_name}`}>
              ⊕ {activeWorktreeSession.worktree_name}
            </span>
          )}
        </div>
      )}

      {/* Terminal panel — visible when expanded */}
      {!collapsed && (
        <div ref={panelRef} className="ws-terminal-panel" style={{ height: panelHeight, flexShrink: 1, minHeight: 0 }}>
          <div className="ws-terminal-resize-handle" onMouseDown={handleResizeMouseDown} />

          <div className="ws-terminal-header">
            <span className="ws-terminal-header-label">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="4 17 10 11 4 5"/>
                <line x1="12" y1="19" x2="20" y2="19"/>
              </svg>
              TERMINAL
            </span>

            {activeWorktreeSession && (
              <span
                className="ws-terminal-worktree-chip"
                title={`In worktree session: ${activeWorktreeSession.worktree_name} → ${activeWorktreeSession.worktree_path}`}
              >
                ⊕ {activeWorktreeSession.worktree_name}
              </span>
            )}

            {/* Branch + worktree dropdown */}
            <div className="ws-terminal-worktree" ref={worktreeMenuRef}>
              <button
                type="button"
                className="ws-terminal-branch-btn"
                onClick={() => setWorktreeOpen((v) => !v)}
                title="Branches and worktrees"
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <line x1="6" y1="3" x2="6" y2="15"/>
                  <circle cx="18" cy="6" r="3"/>
                  <circle cx="6" cy="18" r="3"/>
                  <path d="M18 9a9 9 0 0 1-9 9"/>
                </svg>
                <span className="ws-terminal-branch-text">{branch ?? 'no git'}</span>
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
              </button>
              {worktreeOpen && (
                <div className="ws-terminal-worktree-menu">
                  <div className="ws-terminal-worktree-header">Worktrees</div>
                  {worktrees.length === 0 && (
                    <div className="ws-terminal-worktree-empty">No worktrees found</div>
                  )}
                  {worktrees.map((w) => (
                    <div key={w.path} className={`ws-terminal-worktree-item${w.is_current ? ' current' : ''}`}>
                      <div className="ws-terminal-worktree-info">
                        <div className="ws-terminal-worktree-branch">
                          {w.is_current && <span className="ws-terminal-worktree-dot">●</span>}
                          {w.branch ?? (w.head ? `(detached ${w.head})` : '(bare)')}
                        </div>
                        <div className="ws-terminal-worktree-path">{w.path}</div>
                      </div>
                      {!w.is_current && (
                        <button
                          type="button"
                          className="ws-terminal-worktree-remove"
                          disabled={busy}
                          onClick={() => void handleRemoveWorktree(w.path)}
                          title="Remove worktree"
                        >
                          ×
                        </button>
                      )}
                    </div>
                  ))}
                  <div className="ws-terminal-worktree-divider" />
                  {!showAddForm ? (
                    <button
                      type="button"
                      className="ws-terminal-worktree-add-trigger"
                      onClick={() => setShowAddForm(true)}
                    >
                      + Add worktree
                    </button>
                  ) : (
                    <div className="ws-terminal-worktree-add-form">
                      <input
                        type="text"
                        className="ws-terminal-worktree-input"
                        placeholder="branch name"
                        value={newBranchName}
                        onChange={(e) => setNewBranchName(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') void handleAddWorktree(); }}
                        autoFocus
                      />
                      <label className="ws-terminal-worktree-checkbox">
                        <input
                          type="checkbox"
                          checked={createBranch}
                          onChange={(e) => setCreateBranch(e.target.checked)}
                        />
                        New branch
                      </label>
                      <div className="ws-terminal-worktree-actions">
                        <button
                          type="button"
                          className="ws-terminal-worktree-add-btn"
                          onClick={() => void handleAddWorktree()}
                          disabled={busy || !newBranchName.trim()}
                        >
                          Create
                        </button>
                        <button
                          type="button"
                          className="ws-terminal-worktree-cancel"
                          onClick={() => { setShowAddForm(false); setNewBranchName(''); setCreateBranch(false); }}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="ws-terminal-tabs">
              {sessions.map((tab) => (
                <div
                  key={tab.id}
                  className={`ws-terminal-tab${tab.id === activeTabId ? ' active' : ''}`}
                  onClick={() => handleSwitchTab(tab.id)}
                >
                  <span className="ws-terminal-tab-label">{tab.label}</span>
                  <button
                    className="ws-terminal-tab-close"
                    onClick={(e) => { e.stopPropagation(); handleCloseTab(tab.id); }}
                    type="button"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>

            <div className="ws-terminal-actions">
              <button onClick={refitActive} title="Refit terminal to panel" type="button">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <polyline points="4 14 4 20 10 20"/>
                  <polyline points="20 10 20 4 14 4"/>
                  <line x1="14" y1="10" x2="21" y2="3"/>
                  <line x1="3" y1="21" x2="10" y2="14"/>
                </svg>
              </button>
              <button onClick={() => void handleNewTab()} title="New Terminal" type="button">
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M8 3v10M3 8h10"/>
                </svg>
              </button>
              <button onClick={toggleCollapsed} title="Close Panel" type="button">×</button>
            </div>
          </div>

          <div className="ws-terminal-body" ref={bodyRef}>
            {createError && sessions.length === 0 && (
              <div
                role="alert"
                style={{
                  padding: '12px',
                  font: '13px/1.5 ui-monospace, monospace',
                  color: 'var(--text-muted, #999)',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {`Could not start a terminal: ${createError}\n\n` +
                  'The backend terminal service may be unavailable, or the served frontend ' +
                  'build is stale. Try a hard refresh, or rebuild with `npm install && npm run build`.'}
              </div>
            )}
            {sessions.map((session) => (
              <TerminalTab
                key={session.id}
                ref={setTabHandle(session.id)}
                sessionId={session.id}
                isActive={session.id === activeTabId}
                initialCols={session.cols}
                initialRows={session.rows}
              />
            ))}
          </div>
        </div>
      )}
    </>
  );
};
