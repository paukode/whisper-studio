import React, { useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useUIStore } from '@/stores/uiStore';
import { getEdit, undoEdit, type EditRecord } from '@/api/workspace';
import { CHAT_INSERT_EVENT } from '@/utils/uiEvents';
import { ICON } from './activityIcons';
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
 *
 * Undo shows only for a write that was applied and still has its undo copy
 * (the record's `undoable`). It puts back the file from immediately before
 * this one write (POST /api/workspace/undo); the server refuses, and the
 * toast says why, when the file changed after the write.
 */
export const EditDiffViewer: React.FC = () => {
  const target = useUIStore((s) => s.editDiff);
  if (!target) return null;
  return <EditDiff key={target.toolUseId} toolUseId={target.toolUseId} path={target.path} />;
};

const EditDiff: React.FC<{ toolUseId: string; path: string }> = ({ toolUseId, path }) => {
  const close = useUIStore((s) => s.closeEditDiff);
  const queryClient = useQueryClient();
  const [undoing, setUndoing] = useState(false);
  const { data, error, pending } = useDiffQuery(['workspace-edit', toolUseId], () => getEdit(toolUseId));
  const recordBody = useMemo(() => (data ? bodyFor(data) : null), [data]);

  // Nothing kept for the id: a row from an older transcript, a write that
  // never ran, or a record pruned since.
  const missing = error instanceof ApiError && error.status === 404;
  const kept = data?.stored === true && data.original != null;

  const undo = async () => {
    setUndoing(true);
    const toast = useUIStore.getState().addToast;
    try {
      const done = await undoEdit(toolUseId);
      toast({
        type: 'success',
        message: done.removed ? `Removed ${done.path}, which this change created` : `Undid this change to ${done.path}`,
        duration: 3000,
      });
      window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
      const note = done.removed
        ? `> Note: User undid the write that created \`${done.path}\`. The file was removed.\n\n`
        : `> Note: User undid one write to \`${done.path}\`. The file has its content from before that write.\n\n`;
      window.dispatchEvent(new CustomEvent(CHAT_INSERT_EVENT, { detail: { text: note } }));
      close();
    } catch (err) {
      toast({
        type: 'error',
        message: `Undo failed: ${err instanceof Error ? err.message : String(err)}`,
        duration: 6000,
      });
    } finally {
      setUndoing(false);
      void queryClient.invalidateQueries({ queryKey: ['workspace-edit', toolUseId], exact: true });
    }
  };

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
      actions={data?.undoable ? (
        <button
          type="button"
          className="git-diff-toggle"
          onClick={() => void undo()}
          disabled={undoing}
          title="Put back the file as it was before this change"
        >
          {ICON.undo}Undo
        </button>
      ) : undefined}
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
