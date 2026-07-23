import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getGitChanges, getGitDiff, gitRestoreFile } from '@/api/git';
import type { GitFileStatus as GitFileStatusType } from '@/api/git';
import { GitFileStatus } from './GitFileStatus';
import { useUIStore, dialogConfirm } from '@/stores/uiStore';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { toError } from '@/utils/toError';

const STORAGE_KEY = 'whisper_git_expanded';

export interface GitChangesPanelProps {
  /** Called when a file is clicked to open it in the editor. */
  onFileOpen: (path: string) => void;
}

/**
 * Git changes panel matching the vanilla ws-git-changes structure.
 *
 * Features:
 * - Shows current branch and file count badge
 * - Collapse/expand toggle with localStorage persistence
 * - Per-file line stats (+X/-Y) from /api/git/diff
 * - Discard/restore button per file with confirm dialog
 * - Click file to open in editor
 * - Auto-polls every 15s
 * - Listens for whisper-git-refresh events from SSE
 */
export const GitChangesPanel: React.FC<GitChangesPanelProps> = ({ onFileOpen }) => {
  const queryClient = useQueryClient();

  // Git status loads via react-query. The SSE + event listeners below
  // invalidate ['git-changes'] (debounced) to refetch. react-query dedupes
  // concurrent fetches and coalesces an invalidate-during-fetch into a single
  // trailing refetch — replacing the old manual in-flight/stale tracking.
  const { data, isLoading } = useQuery({
    queryKey: ['git-changes'],
    queryFn: getGitChanges,
  });
  const files = (data?.files ?? []) as GitFileStatusType[];
  const branch = data?.branch ?? '';
  const perFileStats: Record<string, { added: number; removed: number }> = data?.per_file_stats ?? {};

  // HEAD-content modal — populated when user clicks the eye icon on a
  // tracked file row. ``null`` means closed. ``loading`` flag is
  // distinct from ``content === ''`` (an empty HEAD file is valid).
  const [headView, setHeadView] = useState<
    { path: string; content: string | null; loading: boolean; error?: string } | null
  >(null);
  // Persisted via useLocalStorage so multi-tab edits stay in sync via the
  // 'storage' event and we get a single source of truth for the key.
  const [expanded, setExpanded] = useLocalStorage<boolean>(STORAGE_KEY, true);

  const toggleExpand = useCallback(() => {
    setExpanded((prev) => !prev);
  }, [setExpanded]);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 150ms trailing debounce — collapses bursts of refresh events into a
   *  single refetch. */
  const refreshDebounced = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ['git-changes'] });
    }, 150);
  }, [queryClient]);

  // Subscribe to /api/git/events SSE — the backend GitFileWatcher pushes
  // a `git-changed` event within ~1s of any .git/HEAD, config, or branch
  // ref change. Replaces the old 15s polling loop entirely: zero traffic
  // when idle, instant update when something changes.
  useEffect(() => {
    const es = new EventSource('/api/git/events');
    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as { type?: string };
        if (parsed.type === 'git-changed') {
          refreshDebounced();
        }
      } catch {
        // Malformed event — ignore
      }
    };
    return () => es.close();
  }, [refreshDebounced]);

  // User-initiated refresh signals (discard file, approval applied,
  // etc.) still flow through the debounced refetch. These are
  // intra-app — the SSE only covers external git mutations.
  useEffect(() => {
    const handler = () => refreshDebounced();
    window.addEventListener('whisper-git-refresh', handler);
    window.addEventListener('whisper-workspace-refresh', handler);
    return () => {
      window.removeEventListener('whisper-git-refresh', handler);
      window.removeEventListener('whisper-workspace-refresh', handler);
    };
  }, [refreshDebounced]);

  const handleFileSelect = useCallback(
    (path: string) => {
      onFileOpen(path);
    },
    [onFileOpen],
  );

  const handleViewHead = useCallback(async (path: string) => {
    setHeadView({ path, content: null, loading: true });
    try {
      const res = await getGitDiff(path);
      setHeadView({
        path,
        content: res.content,
        loading: false,
        error: res.error,
      });
    } catch (err) {
      setHeadView({
        path,
        content: null,
        loading: false,
        error: toError(err).message,
      });
    }
  }, []);

  const closeHeadView = useCallback(() => setHeadView(null), []);

  // Esc closes the HEAD modal — matches the rest of the app's modal
  // dismissal pattern (SettingsModal, MemoryEditorModal, etc.).
  useEffect(() => {
    if (!headView) return;
    // document, not window: keeps this dismisser ahead of window-level
    // global shortcuts (the ESC stream kill switch) regardless of
    // registration order, matching the other Escape dismissers.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); closeHeadView(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [headView, closeHeadView]);

  const handleDiscard = useCallback(
    async (path: string) => {
      const name = path.split('/').pop() ?? path;
      const ok = await dialogConfirm({ message: `Discard changes to ${name}?`, danger: true, confirmText: 'Discard' });
      if (!ok) return;
      try {
        await gitRestoreFile(path);
        // Await the status refresh so the toast and the panel agree. The
        // previous fire-and-forget call let the toast claim "Discarded"
        // before the panel reflected the file as clean — and on a silent
        // git-restore failure the panel would still show it as modified.
        await queryClient.refetchQueries({ queryKey: ['git-changes'] });
        useUIStore.getState().addToast({ type: 'success', message: `Discarded changes to ${name}`, duration: 2000 });
      } catch (err) {
        useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
      }
    },
    [queryClient],
  );

  const stagedFiles = files.filter((f) => f.staged);
  const unstagedFiles = files.filter((f) => !f.staged);
  const totalCount = files.length;

  return (
    <div className={`ws-git-changes${expanded ? ' expanded' : ''}`} style={{ display: totalCount > 0 || isLoading ? undefined : 'none' }}>
      {/* Header — click to collapse/expand */}
      <div className="ws-git-changes-header" onClick={toggleExpand} role="button" tabIndex={0}>
        <span className={`file-tree-chevron${expanded ? ' file-tree-chevron-expanded' : ''}`} style={{ fontSize: '8px', marginRight: 4 }}>
          ▶
        </span>
        <span style={{ fontWeight: 600, fontSize: '11px' }}>Git Changes</span>
        {branch && (
          <span className="ws-git-branch" title={branch}>
            {branch}
          </span>
        )}
        {totalCount > 0 && (
          <span className="ws-git-change-count">{totalCount}</span>
        )}
        <button
          className="ws-git-refresh-btn"
          onClick={(e) => { e.stopPropagation(); void queryClient.refetchQueries({ queryKey: ['git-changes'] }); }}
          disabled={isLoading}
          title="Refresh"
          type="button"
          style={{ marginLeft: 'auto' }}
        >
          ↻
        </button>
      </div>

      {/* HEAD viewer — small read-only modal showing the version of
       *  the file as it exists at HEAD. Click outside or hit Esc to
       *  close. No editing affordances. */}
      {headView && (
        <div
          className="git-head-modal-backdrop"
          onClick={(e) => { if (e.target === e.currentTarget) closeHeadView(); }}
        >
          <div className="git-head-modal" role="dialog" aria-modal="true">
            <div className="git-head-modal-header">
              <span className="git-head-modal-title">
                HEAD · <code>{headView.path}</code>
              </span>
              <button
                type="button"
                className="git-head-modal-close"
                onClick={closeHeadView}
                aria-label="Close"
              >
                ✕
              </button>
            </div>
            <div className="git-head-modal-body">
              {headView.loading && (
                <div className="git-head-modal-info" aria-busy="true">
                  <span className="skeleton skeleton-text" style={{ width: '85%' }} />
                  <span className="skeleton skeleton-text" style={{ width: '70%' }} />
                </div>
              )}
              {!headView.loading && headView.error && (
                <div className="git-head-modal-error">
                  Could not load HEAD version: {headView.error}
                </div>
              )}
              {!headView.loading && !headView.error && headView.content !== null && (
                <pre className="git-head-modal-pre">{headView.content}</pre>
              )}
              {!headView.loading && !headView.error && headView.content === null && (
                <div className="git-head-modal-info">No HEAD content available.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* File list — visibility controlled by .expanded class in CSS */}
      <div className="ws-git-changes-list">
        {isLoading && files.length === 0 && (
          <div style={{ padding: '8px 14px' }} aria-busy="true">
            <span className="skeleton skeleton-text" style={{ width: '75%' }} />
            <span className="skeleton skeleton-text" style={{ width: '55%' }} />
          </div>
        )}

        {!isLoading && files.length === 0 && (
          <div style={{ padding: '8px 14px', fontSize: '11px', color: 'var(--text-muted)' }}>No changes</div>
        )}

        {stagedFiles.length > 0 && (
          <>
            <div className="ws-git-section-title">Staged ({stagedFiles.length})</div>
            {stagedFiles.map((file) => {
              const stats = perFileStats[file.path];
              return (
                <GitFileStatus
                  key={`s-${file.path}`}
                  path={file.path}
                  status={file.status}
                  staged={file.staged}
                  added={stats?.added}
                  removed={stats?.removed}
                  onSelect={handleFileSelect}
                  onViewHead={handleViewHead}
                />
              );
            })}
          </>
        )}

        {unstagedFiles.length > 0 && (
          <>
            {stagedFiles.length > 0 && (
              <div className="ws-git-section-title">Changes ({unstagedFiles.length})</div>
            )}
            {unstagedFiles.map((file) => {
              const stats = perFileStats[file.path];
              return (
                <GitFileStatus
                  key={`u-${file.path}`}
                  path={file.path}
                  status={file.status}
                  staged={file.staged}
                  added={stats?.added}
                  removed={stats?.removed}
                  onSelect={handleFileSelect}
                  onDiscard={handleDiscard}
                  onViewHead={handleViewHead}
                />
              );
            })}
          </>
        )}
      </div>
    </div>
  );
};
