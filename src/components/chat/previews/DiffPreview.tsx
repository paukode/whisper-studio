import { useMemo } from 'react';
import { DiffGrid } from '@/components/git/DiffDialog';
import { collapseUnchanged, rowsFromTexts, type DiffRow } from '@/utils/diffRows';

export interface DiffPreviewProps {
  /** May be undefined for create (no original) */
  original?: string;
  content?: string;
  path?: string;
}

/**
 * The approval card's diff: the diff dialog's own grid (DiffGrid), unified
 * and without the dialog chrome, under a path line with the +added -removed
 * counts.
 */
export function DiffPreview({ original, content, path }: DiffPreviewProps) {
  const diff = useMemo((): { rows: DiffRow[]; added: number; removed: number } | null => {
    if (original == null && content == null) return null;
    let rows: DiffRow[];
    try {
      rows = rowsFromTexts(original ?? '', content ?? '');
    } catch {
      // Size guard (DiffTooLargeError): the texts differ throughout and the
      // alignment table would not fit. The card shows the new text instead,
      // so the approval stays answerable.
      return null;
    }
    let added = 0;
    let removed = 0;
    for (const row of rows) {
      if (row.kind !== 'change') continue;
      if (row.left) removed += 1;
      if (row.right) added += 1;
    }
    return { rows: collapseUnchanged(rows), added, removed };
  }, [original, content]);

  const added = diff?.added ?? 0;
  const removed = diff?.removed ?? 0;
  // A write that leaves the file as it was: the grid is one collapsed gap
  // with no counts, so say so in words above it.
  const noChanges = diff !== null && diff.rows.length > 0 && added === 0 && removed === 0;

  return (
    <div>
      {path && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6, fontFamily: 'Menlo, monospace' }}>
          {path}
          {(added > 0 || removed > 0) && (
            <span style={{ marginLeft: 8 }}>
              <span style={{ color: 'var(--accent-live, #34c77b)' }}>+{added}</span>
              {' '}
              <span style={{ color: 'var(--accent-record, #e5453a)' }}>-{removed}</span>
            </span>
          )}
        </div>
      )}
      {noChanges && (
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>No changes</div>
      )}
      {!diff || diff.rows.length === 0 ? (
        <pre style={{
          fontSize: 12, fontFamily: 'Menlo, monospace',
          background: 'var(--bg-inset)', padding: 8, borderRadius: 4,
          maxHeight: 240, overflow: 'auto', whiteSpace: 'pre-wrap',
        }}>{content ?? ''}</pre>
      ) : (
        <div className="git-diff-preview">
          <DiffGrid rows={diff.rows} layout="unified" />
        </div>
      )}
    </div>
  );
}
