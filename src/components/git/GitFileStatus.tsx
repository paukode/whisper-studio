import React, { useCallback } from 'react';

export interface GitFileStatusProps {
  path: string;
  status: string;
  staged: boolean;
  added?: number;
  removed?: number;
  onSelect: (path: string) => void;
  onDiscard?: (path: string) => void;
  /** Opens the side-by-side diff. Wired to the status letter, which
   *  works for every status: untracked files read as all additions,
   *  deleted ones as all removals. */
  onViewDiff?: (path: string) => void;
}

const STATUS_LABELS: Record<string, string> = {
  modified: 'M',
  added: 'A',
  deleted: 'D',
  renamed: 'R',
  copied: 'C',
  untracked: '?',
  M: 'M',
  A: 'A',
  D: 'D',
  R: 'R',
  C: 'C',
  '?': '?',
};

/**
 * Individual file status row in the git changes panel.
 * Displays status badge, file path, per-file line stats, and discard button.
 * Matches the vanilla ws-git-change-item structure.
 *
 * Two targets on one row: the row opens the file in the editor, its
 * status letter opens the diff.
 */
export const GitFileStatus: React.FC<GitFileStatusProps> = ({
  path,
  status,
  staged,
  added,
  removed,
  onSelect,
  onDiscard,
  onViewDiff,
}) => {
  const handleClick = useCallback(() => {
    onSelect(path);
  }, [path, onSelect]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onSelect(path);
      }
    },
    [path, onSelect],
  );

  const handleDiscard = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onDiscard?.(path);
    },
    [path, onDiscard],
  );

  const handleViewDiff = useCallback(
    (e: React.MouseEvent) => {
      // Without this the row's own handler would also fire and open the
      // editor behind the diff.
      e.stopPropagation();
      onViewDiff?.(path);
    },
    [path, onViewDiff],
  );

  const statusLabel = STATUS_LABELS[status] ?? status.charAt(0).toUpperCase();
  const fileName = path.split('/').pop() ?? path;
  const dirPath = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : '';

  return (
    <div
      className={`ws-git-change-item${staged ? ' git-file-staged' : ''}`}
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      title={path}
      aria-label={`${path}: ${status}${staged ? ', staged' : ''}`}
    >
      {onViewDiff ? (
        <button
          type="button"
          className={`ws-git-status-badge status-${statusLabel}`}
          onClick={handleViewDiff}
          title="View changes"
          aria-label={`View changes in ${path}`}
        >
          {statusLabel}
        </button>
      ) : (
        <span className={`ws-git-status-badge status-${statusLabel}`} aria-hidden="true">
          {statusLabel}
        </span>
      )}
      <span className="ws-git-change-path">
        {dirPath && <span className="ws-git-change-dir">{dirPath}</span>}
        <span className="ws-git-change-name">{fileName}</span>
      </span>
      {(added !== undefined && added > 0 || removed !== undefined && removed > 0) && (
        <span className="ws-git-change-stats">
          {added !== undefined && added > 0 && <span className="added">+{added}</span>}
          {added !== undefined && added > 0 && removed !== undefined && removed > 0 && ' '}
          {removed !== undefined && removed > 0 && <span className="removed">-{removed}</span>}
        </span>
      )}
      {onDiscard && !staged && (
        <button
          className="ws-git-change-undo"
          title="Discard changes"
          onClick={handleDiscard}
          type="button"
        >
          ↻
        </button>
      )}
    </div>
  );
};
