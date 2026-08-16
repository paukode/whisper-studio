import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getGitFileDiff } from '@/api/git';
import { useResizableDialog } from '@/hooks/useResizableDialog';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import {
  collapseUnchanged,
  rowsFromHunks,
  rowsFromTexts,
  type DiffRow,
} from '@/utils/diffRows';

export interface GitDiffViewerProps {
  /** Workspace-relative path, as reported by /api/git/changes. Key the
   *  element on it so switching files gets a clean viewer. */
  path: string;
  onClose: () => void;
}

type Layout = 'split' | 'unified';

const STATUS_WORDS: Record<string, string> = {
  modified: 'Modified',
  added: 'Added',
  deleted: 'Deleted',
};

/**
 * Side-by-side (or unified) diff of one file against HEAD.
 *
 * Opened from the status letter in the Git Changes panel. Rows are the
 * alignment unit — both sides share one grid, so the panes cannot drift
 * apart however the lines wrap. Rendering mode comes from the server:
 * whole-file alignment for normal files, git's own hunks for files too
 * large to align in the browser.
 */
export const GitDiffViewer: React.FC<GitDiffViewerProps> = ({ path, onClose }) => {
  const { dialogRef, style, onMoveStart, onResizeStart, reset } = useResizableDialog(
    STORAGE_KEYS.GIT_DIFF_GEOMETRY,
    { defaultW: 900, minW: 420, minH: 300 },
  );
  const [layout, setLayout] = useLocalStorage<Layout>(STORAGE_KEYS.GIT_DIFF_LAYOUT, 'split');
  const [openGaps, setOpenGaps] = useState<Set<number>>(() => new Set());

  const queryClient = useQueryClient();
  const queryKey = useMemo(() => ['git-file-diff', path], [path]);

  const { data, isPending, error, isFetching, fetchStatus } = useQuery({
    queryKey,
    queryFn: () => getGitFileDiff(path),
    // A diff is a snapshot of the working tree; refetching on window focus
    // would swap the content out from under someone mid-read.
    refetchOnWindowFocus: false,
  });

  // A retry only proceeds while the window has focus (query-core gates it on
  // focusManager as well as the network), so a first attempt that fails just
  // as the user tabs away leaves the query parked in `paused`: pending, not
  // fetching, no error, no data. That combination used to satisfy no branch
  // below at all and the dialog sat blank with nothing to act on. Treat a
  // paused query as a stall the reader can retry out of.
  const stalled = isPending && fetchStatus === 'paused';
  const isLoading = isPending && isFetching;

  // Not refetch(): with no data cached, Query.fetch sees a non-idle
  // fetchStatus and hands back the parked retryer promise instead of
  // starting anything, so the button would do nothing in exactly the case
  // it exists for. Resetting drops the stuck retryer and refetches clean.
  const retry = useCallback(() => {
    void queryClient.resetQueries({ queryKey, exact: true });
  }, [queryClient, queryKey]);

  // Esc closes. document, not window, so this stays ahead of the global
  // ESC stream kill switch regardless of registration order.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const { rows, tooLarge } = useMemo((): { rows: DiffRow[]; tooLarge: boolean } => {
    if (!data) return { rows: [], tooLarge: false };
    if (data.mode === 'full') {
      try {
        return {
          rows: collapseUnchanged(rowsFromTexts(data.old ?? '', data.new ?? '')),
          tooLarge: false,
        };
      } catch {
        // The server sends `full` only within the client's alignment
        // budget, so this is a belt-and-braces path.
        return { rows: [], tooLarge: true };
      }
    }
    if (data.mode === 'hunks') return { rows: rowsFromHunks(data.hunks), tooLarge: false };
    return { rows: [], tooLarge: false };
  }, [data]);

  // Expanded gaps render their hidden rows in place of the marker.
  const visible = useMemo(
    () =>
      rows.flatMap((row, i) =>
        row.kind === 'gap' && openGaps.has(i)
          ? row.rows.map((r) => ({ row: r, gapIndex: -1 }))
          : [{ row, gapIndex: i }],
      ),
    [rows, openGaps],
  );

  const toggleGap = useCallback((index: number) => {
    setOpenGaps((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const fileName = path.split('/').pop() ?? path;

  return (
    <div
      className="git-diff-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="git-diff-dialog" ref={dialogRef} style={style} role="dialog" aria-modal="true">
        <div className="git-diff-header" onPointerDown={onMoveStart}>
          <span className="git-diff-title" title={path}>
            {fileName}
            {data && <span className="git-diff-status">{STATUS_WORDS[data.status] ?? ''}</span>}
          </span>
          {data && (data.added > 0 || data.removed > 0) && (
            <span className="git-diff-stats">
              <span className="added">+{data.added}</span> <span className="removed">-{data.removed}</span>
            </span>
          )}
          <div className="git-diff-actions" onPointerDown={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="git-diff-toggle"
              onClick={() => setLayout(layout === 'split' ? 'unified' : 'split')}
              title={layout === 'split' ? 'Switch to unified view' : 'Switch to side-by-side view'}
            >
              {layout === 'split' ? 'Unified' : 'Side by side'}
            </button>
            <button
              type="button"
              className="git-diff-toggle"
              onClick={reset}
              title="Reset size and position"
            >
              Reset
            </button>
            <button type="button" className="git-diff-close" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </div>
        </div>

        <div className="git-diff-path" title={path}>
          {path}
        </div>

        <div className="git-diff-body">
          {isLoading && (
            <div className="git-diff-info" aria-busy="true">
              <span className="skeleton skeleton-text" style={{ width: '80%' }} />
              <span className="skeleton skeleton-text" style={{ width: '60%' }} />
            </div>
          )}

          {/* The fetch failed, or stalled mid-retry. Show what actually went
              wrong — a dropped connection and a rejected path are different
              problems and want different responses from the reader — and
              offer the retry, since a transport blip succeeds on the next
              attempt and a paused query needs the nudge to resume at all. */}
          {!isLoading && (error || stalled) && (
            <div className="git-diff-error">
              <span>
                {stalled && !error
                  ? 'Lost the connection to the backend before the diff arrived.'
                  : error instanceof Error
                    ? error.message
                    : 'Could not load diff.'}
              </span>
              <button
                type="button"
                className="git-diff-retry"
                onClick={retry}
                disabled={isFetching}
              >
                {isFetching ? 'Retrying…' : 'Retry'}
              </button>
            </div>
          )}

          {!isLoading && !error && data?.mode === 'error' && (
            <div className="git-diff-error">{data.error ?? 'Could not load diff.'}</div>
          )}

          {!isLoading && !error && data?.mode === 'binary' && (
            <div className="git-diff-info">Binary file — no line-by-line diff.</div>
          )}

          {!isLoading && !error && tooLarge && (
            <div className="git-diff-info">This file is too large to diff in the browser.</div>
          )}

          {!isLoading && !error && data && (data.mode === 'full' || data.mode === 'hunks') && (
            <>
              {data.truncated && (
                <div className="git-diff-notice">
                  Long diff — showing the first part only.
                </div>
              )}
              {visible.length === 0 ? (
                <div className="git-diff-info">No changes against HEAD.</div>
              ) : (
                <div className={`git-diff-grid ${layout}`}>
                  {visible.map(({ row, gapIndex }, i) =>
                    row.kind === 'gap' ? (
                      <GapRow
                        key={`g-${i}`}
                        count={row.count}
                        expandable={row.rows.length > 0}
                        onExpand={() => toggleGap(gapIndex)}
                      />
                    ) : layout === 'split' ? (
                      <SplitRow key={i} row={row} />
                    ) : (
                      <UnifiedRows key={i} row={row} />
                    ),
                  )}
                </div>
              )}
            </>
          )}
        </div>

        <div className="ws-rz ws-rz-e" onPointerDown={onResizeStart({ e: true })} />
        <div className="ws-rz ws-rz-s" onPointerDown={onResizeStart({ s: true })} />
        <div className="ws-rz ws-rz-se" onPointerDown={onResizeStart({ e: true, s: true })} />
      </div>
    </div>
  );
};

/** One grid row: old line left, new line right. */
const SplitRow: React.FC<{ row: Extract<DiffRow, { kind: 'unchanged' | 'change' }> }> = ({ row }) => {
  const removed = row.kind === 'change' && row.left !== null;
  const added = row.kind === 'change' && row.right !== null;
  return (
    <>
      <div className="git-diff-num left">{row.left?.no ?? ''}</div>
      <div className={`git-diff-code${removed ? ' removed' : ''}`}>
        <span className="git-diff-sign" aria-hidden="true">{removed ? '-' : ' '}</span>
        {row.left?.text ?? ''}
      </div>
      <div className="git-diff-num right">{row.right?.no ?? ''}</div>
      <div className={`git-diff-code${added ? ' added' : ''}`}>
        <span className="git-diff-sign" aria-hidden="true">{added ? '+' : ' '}</span>
        {row.right?.text ?? ''}
      </div>
    </>
  );
};

/** Unified view: a replaced line becomes two stacked rows. */
const UnifiedRows: React.FC<{ row: Extract<DiffRow, { kind: 'unchanged' | 'change' }> }> = ({ row }) => {
  if (row.kind === 'unchanged') {
    return (
      <>
        <div className="git-diff-num">{row.left.no}</div>
        <div className="git-diff-num">{row.right.no}</div>
        <div className="git-diff-code">
          <span className="git-diff-sign" aria-hidden="true">{' '}</span>
          {row.left.text}
        </div>
      </>
    );
  }
  return (
    <>
      {row.left && (
        <>
          <div className="git-diff-num">{row.left.no}</div>
          <div className="git-diff-num" />
          <div className="git-diff-code removed">
            <span className="git-diff-sign" aria-hidden="true">-</span>
            {row.left.text}
          </div>
        </>
      )}
      {row.right && (
        <>
          <div className="git-diff-num" />
          <div className="git-diff-num">{row.right.no}</div>
          <div className="git-diff-code added">
            <span className="git-diff-sign" aria-hidden="true">+</span>
            {row.right.text}
          </div>
        </>
      )}
    </>
  );
};

/** Collapsed run of unchanged lines. Expandable only when the client
 *  holds the hidden content (whole-file mode). */
const GapRow: React.FC<{ count: number; expandable: boolean; onExpand: () => void }> = ({
  count,
  expandable,
  onExpand,
}) => {
  const label = `${count} unchanged ${count === 1 ? 'line' : 'lines'}`;
  return (
    <div className="git-diff-gap">
      {expandable ? (
        <button type="button" className="git-diff-gap-btn" onClick={onExpand}>
          ⋯ {label}
        </button>
      ) : (
        <span className="git-diff-gap-label">⋯ {label}</span>
      )}
    </div>
  );
};
