import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useResizableDialog } from '@/hooks/useResizableDialog';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import { ICON } from '@/components/chat/activityIcons';
import type { DiffRow, DiffSide } from '@/utils/diffRows';

export type DiffStatus = 'modified' | 'added' | 'deleted';
export type DiffLayout = 'split' | 'unified';

/** What the dialog body shows: the rows, or one state in their place. */
export type DiffBody =
  | { kind: 'rows'; rows: DiffRow[]; /** Shown when there are no rows. */ empty: string }
  | { kind: 'loading' }
  | { kind: 'error'; message: string; onRetry?: () => void; retrying?: boolean }
  | { kind: 'info'; text: string };

export interface DiffDialogProps {
  /** The path on the line under the header; its last part is the title. */
  path: string;
  /** Left out until the diff is known. */
  status?: DiffStatus;
  added?: number | null;
  removed?: number | null;
  /** The counts are an estimate: the server's exact diff ran over its bounds. */
  approximate?: boolean;
  /** The rows hold only the first part of a long diff. */
  truncated?: boolean;
  body: DiffBody;
  onClose: () => void;
}

/** What Tab moves through inside the dialog. */
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])';

const STATUS_WORDS: Record<DiffStatus, string> = {
  modified: 'Modified',
  added: 'Added',
  deleted: 'Deleted',
};

/**
 * Side-by-side (or unified) diff of one file: the Git panel's dialog, also
 * opened from an activity row's diff pill. The caller fetches and builds the
 * rows (GitDiffViewer against HEAD, EditDiffViewer from a stored edit); this
 * draws them, with the Split or Unified choice and the size and position
 * kept in localStorage. Rows are the alignment unit: both sides share one
 * grid, so the panes cannot drift apart however the lines wrap.
 */
export const DiffDialog: React.FC<DiffDialogProps> = ({
  path,
  status,
  added,
  removed,
  approximate = false,
  truncated = false,
  body,
  onClose,
}) => {
  const { dialogRef, style, onMoveStart, onResizeStart, reset } = useResizableDialog(
    STORAGE_KEYS.GIT_DIFF_GEOMETRY,
    { defaultW: 900, minW: 420, minH: 300 },
  );
  const [layout, setLayout] = useLocalStorage<DiffLayout>(STORAGE_KEYS.GIT_DIFF_LAYOUT, 'split');
  const closeRef = useRef<HTMLButtonElement>(null);

  // A modal takes the focus while it is open (its close button first) and
  // gives it back on close to what had it: the Git panel's row, or the
  // activity row's diff pill.
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => previous?.focus();
  }, []);

  // Esc closes, and Tab stays inside the dialog. document, not window, so
  // this stays ahead of the global ESC stream kill switch regardless of
  // registration order.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const root = dialogRef.current;
      if (!root) return;
      const focusable = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!root.contains(active) || (e.shiftKey ? active === first : active === last)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, dialogRef]);

  const fileName = path.split('/').pop() || path;
  const showStats = (added ?? 0) > 0 || (removed ?? 0) > 0;

  return (
    <div
      className="git-diff-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="git-diff-dialog"
        ref={dialogRef}
        style={style}
        role="dialog"
        aria-modal="true"
        aria-label={`Diff of ${fileName}`}
      >
        <div className="git-diff-header" onPointerDown={onMoveStart}>
          <span className="git-diff-title" title={path}>
            {fileName}
            {status && <span className="git-diff-status">{STATUS_WORDS[status]}</span>}
          </span>
          {showStats && (
            <span className="git-diff-stats">
              <span className="added">+{added ?? 0}</span> <span className="removed">-{removed ?? 0}</span>
            </span>
          )}
          {showStats && approximate && (
            <span className="git-diff-approx" title="These counts are an estimate: the exact count took too long">
              approximate
            </span>
          )}
          <div className="git-diff-actions" onPointerDown={(e) => e.stopPropagation()}>
            <button
              type="button"
              className={`git-diff-toggle${layout === 'split' ? ' active' : ''}`}
              aria-pressed={layout === 'split'}
              onClick={() => setLayout('split')}
              title="Old and new side by side"
            >
              Split
            </button>
            <button
              type="button"
              className={`git-diff-toggle${layout === 'unified' ? ' active' : ''}`}
              aria-pressed={layout === 'unified'}
              onClick={() => setLayout('unified')}
              title="Old and new in one column"
            >
              Unified
            </button>
            <button type="button" className="git-diff-toggle" onClick={reset} title="Reset size and position">
              Reset
            </button>
            <button ref={closeRef} type="button" className="git-diff-close" onClick={onClose} aria-label="Close" title="Close">
              {ICON.x}
            </button>
          </div>
        </div>

        <div className="git-diff-path" title={path}>
          {path}
        </div>

        <div className="git-diff-body">
          {body.kind === 'loading' && (
            <div className="git-diff-info" aria-busy="true">
              <span className="skeleton skeleton-text" style={{ width: '80%' }} />
              <span className="skeleton skeleton-text" style={{ width: '60%' }} />
            </div>
          )}

          {/* Show what actually went wrong: a dropped connection and a
              rejected path are different problems and want different
              responses from the reader. The retry is there when the caller
              offers one, since a transport blip succeeds on the next attempt
              and a paused query needs the nudge to resume at all. */}
          {body.kind === 'error' && (
            <div className="git-diff-error">
              <span>{body.message}</span>
              {body.onRetry && (
                <button
                  type="button"
                  className="git-diff-retry"
                  onClick={body.onRetry}
                  disabled={body.retrying}
                >
                  {body.retrying ? 'Retrying...' : 'Retry'}
                </button>
              )}
            </div>
          )}

          {body.kind === 'info' && <div className="git-diff-info">{body.text}</div>}

          {body.kind === 'rows' && (
            <>
              {truncated && (
                <div className="git-diff-notice">Long diff: showing the first part only.</div>
              )}
              {body.rows.length === 0 ? (
                <div className="git-diff-info">{body.empty}</div>
              ) : (
                <DiffGrid rows={body.rows} layout={layout} />
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

/**
 * The diff grid alone, with no dialog chrome: the dialog's body, and the
 * approval card's preview (unified). The app's one diff renderer for chat,
 * approvals and the Git panel. A gap row expands in place when it holds the
 * hidden lines (rows built from both texts); a gap from git's hunks has none
 * to show.
 */
export const DiffGrid: React.FC<{ rows: DiffRow[]; layout: DiffLayout }> = ({ rows, layout }) => {
  const [openGaps, setOpenGaps] = useState<ReadonlySet<number>>(() => new Set());
  // Gaps are opened by index, so new rows (the next approval card, a
  // refetched diff) start with every gap closed.
  const [gapRows, setGapRows] = useState(rows);
  if (gapRows !== rows) {
    setGapRows(rows);
    setOpenGaps(new Set());
  }

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

  return (
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
  );
};

/** What a line whose ending alone changed says after its text
 *  (DiffSide.eol): that it has no final newline, or that it ends in CRLF. */
const EolNote: React.FC<{ side: DiffSide | null }> = ({ side }) => {
  if (!side?.eol) return null;
  return <span className="git-diff-eol">{side.eol === 'none' ? 'No newline at end of file' : 'CRLF'}</span>;
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
        <EolNote side={row.left} />
      </div>
      <div className="git-diff-num right">{row.right?.no ?? ''}</div>
      <div className={`git-diff-code${added ? ' added' : ''}`}>
        <span className="git-diff-sign" aria-hidden="true">{added ? '+' : ' '}</span>
        {row.right?.text ?? ''}
        <EolNote side={row.right} />
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
            <EolNote side={row.left} />
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
            <EolNote side={row.right} />
          </div>
        </>
      )}
    </>
  );
};

/** Collapsed run of unchanged lines. Expandable only when the client
 *  holds the hidden content (rows built from both texts). */
const GapRow: React.FC<{ count: number; expandable: boolean; onExpand: () => void }> = ({
  count,
  expandable,
  onExpand,
}) => {
  const label = `... ${count} unchanged ${count === 1 ? 'line' : 'lines'}`;
  return (
    <div className="git-diff-gap">
      {expandable ? (
        <button type="button" className="git-diff-gap-btn" onClick={onExpand}>
          {label}
        </button>
      ) : (
        <span className="git-diff-gap-label">{label}</span>
      )}
    </div>
  );
};
