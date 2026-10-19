import React, { useMemo } from 'react';
import { useUIStore } from '@/stores/uiStore';
import { getEdit, type EditRecord } from '@/api/workspace';
import { ApiError } from '@/types/api';
import { DiffDialog, type DiffBody } from '@/components/git/DiffDialog';
import { useDiffQuery } from '@/components/git/useDiffQuery';
import { collapseUnchanged, rowsFromTexts } from '@/utils/diffRows';

const NOT_KEPT: DiffBody = { kind: 'info', text: 'The diff for this edit was not kept.' };

/**
 * The diff of one edit the assistant made, opened from the diff pill of its
 * activity row (uiStore.editDiff). It is the Git panel's own dialog, fed
 * with the file before and after that call as the server kept them
 * (GET /api/workspace/edits/{tool_use_id}), so it shows that one edit even
 * after later writes or a commit changed the file. Mounted once, in the
 * app shell.
 */
export const EditDiffViewer: React.FC = () => {
  const target = useUIStore((s) => s.editDiff);
  if (!target) return null;
  return <EditDiff key={target.toolUseId} toolUseId={target.toolUseId} path={target.path} />;
};

const EditDiff: React.FC<{ toolUseId: string; path: string }> = ({ toolUseId, path }) => {
  const close = useUIStore((s) => s.closeEditDiff);
  const { data, error, pending } = useDiffQuery(['workspace-edit', toolUseId], () => getEdit(toolUseId));
  const recordBody = useMemo(() => (data ? bodyFor(data) : null), [data]);

  // Nothing kept for the id: a row from an older transcript, a write that
  // never ran, or a record pruned since.
  const missing = error instanceof ApiError && error.status === 404;
  const kept = data?.stored === true && data.original != null;

  return (
    <DiffDialog
      // The absolute path the edit wrote, as the record kept it, so a later
      // change of workspace cannot move it. The pill's own path (absolute
      // once joined to the workspace) until the record lands, on a 404, and
      // for a record kept before it held one.
      path={data?.full_path || path}
      // A created file opens against an empty original: every line added.
      // An existing empty file the write filled has one too, and was modified.
      status={kept ? (data.created ? 'added' : 'modified') : undefined}
      added={data?.added}
      removed={data?.removed}
      approximate={data?.approximate}
      body={missing ? NOT_KEPT : pending ?? recordBody ?? { kind: 'loading' }}
      onClose={close}
    />
  );
};

function bodyFor(record: EditRecord): DiffBody {
  // A text over the size cap, or a dotenv or credential file: the record
  // keeps the counts and not the texts.
  if (!record.stored || record.original == null || record.modified == null) return NOT_KEPT;
  try {
    return {
      kind: 'rows',
      rows: collapseUnchanged(rowsFromTexts(record.original, record.modified)),
      empty: 'The file is empty.',
    };
  } catch {
    // DiffTooLargeError: the texts differ throughout and the alignment
    // table would not fit in the browser.
    return { kind: 'info', text: 'This file is too large to diff in the browser.' };
  }
}
