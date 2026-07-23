import React, { useCallback } from 'react';

export interface GitFileStatusProps {
  path: string;
  status: string;
  staged: boolean;
  added?: number;
  removed?: number;
  onSelect: (path: string) => void;
  onDiscard?: (path: string) => void;
  /** Optional "View HEAD" affordance — only meaningful for tracked
   *  files (untracked have no HEAD version). Parent decides eligibility. */
  onViewHead?: (path: string) => void;
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
 */
export const GitFileStatus: React.FC<GitFileStatusProps> = ({
  path,
  status,
  staged,
  added,
  removed,
  onSelect,
  onDiscard,
  onViewHead,
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

  const handleViewHead = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onViewHead?.(path);
    },
    [path, onViewHead],
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
      <span className={`ws-git-status-badge status-${statusLabel}`} aria-hidden="true">
        {statusLabel}
      </span>
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
      {/* View HEAD is only meaningful for tracked files (status !== '?'),
       *  since untracked paths have no HEAD revision to fetch. */}
      {onViewHead && statusLabel !== '?' && (
        <button
          className="ws-git-change-view-head"
          title="View HEAD version"
          onClick={handleViewHead}
          type="button"
          aria-label={`View HEAD version of ${path}`}
        >
          👁
        </button>
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
