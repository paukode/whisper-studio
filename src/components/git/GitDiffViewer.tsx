import React, { useMemo } from 'react';
import { getGitFileDiff, type GitFileDiff } from '@/api/git';
import {
  collapseUnchanged,
  rowsFromHunks,
  rowsFromTexts,
  type DiffRow,
} from '@/utils/diffRows';
import { DiffDialog, type DiffBody } from './DiffDialog';
import { useDiffQuery } from './useDiffQuery';

export interface GitDiffViewerProps {
  /** Workspace-relative path, as reported by /api/git/changes. Key the
   *  element on it so switching files gets a clean viewer. */
  path: string;
  onClose: () => void;
}

/**
 * Diff of one file against HEAD, opened from the status letter in the Git
 * Changes panel. Rendering mode comes from the server: whole-file alignment
 * for normal files, git's own hunks for files too large to align in the
 * browser. DiffDialog draws it.
 */
export const GitDiffViewer: React.FC<GitDiffViewerProps> = ({ path, onClose }) => {
  const { data, pending } = useDiffQuery(['git-file-diff', path], () => getGitFileDiff(path));
  const rows = useMemo(() => (data ? rowsFor(data) : null), [data]);

  return (
    <DiffDialog
      path={path}
      status={data?.status}
      added={data?.added}
      removed={data?.removed}
      truncated={data?.truncated}
      body={pending ?? bodyFor(data, rows)}
      onClose={onClose}
    />
  );
};

/** The rows of a `full` or `hunks` payload; null for the other modes and
 *  for texts too large to align. */
function rowsFor(data: GitFileDiff): DiffRow[] | null {
  if (data.mode === 'hunks') return rowsFromHunks(data.hunks);
  if (data.mode !== 'full') return null;
  try {
    return collapseUnchanged(rowsFromTexts(data.old ?? '', data.new ?? ''));
  } catch {
    // The server sends `full` only within the client's alignment
    // budget, so this is a belt-and-braces path.
    return null;
  }
}

function bodyFor(data: GitFileDiff, rows: DiffRow[] | null): DiffBody {
  if (data.mode === 'error') return { kind: 'error', message: data.error ?? 'Could not load diff.' };
  if (data.mode === 'binary') return { kind: 'info', text: 'Binary file: no line-by-line diff.' };
  if (rows === null) return { kind: 'info', text: 'This file is too large to diff in the browser.' };
  return { kind: 'rows', rows, empty: 'No changes against HEAD.' };
}
