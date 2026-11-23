import React, { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { removeModelFromList } from '@/api/modelBrowser';
import { useUIStore } from '@/stores/uiStore';
import { humanSize } from '@/utils/humanSize';
import { ConfirmDialog } from './ConfirmDialog';
import { refreshModelLists } from './refreshModelLists';

/** The one Delete for an on-device chat model, the same in Models > Chat and
 *  in Discover: it deletes the weights AND the config entry, so the model
 *  leaves every list at once. */
export const DeleteLocalModelButton: React.FC<{
  modelKey: string;
  label: string;
  /** Bytes the delete frees, when known. */
  bytes?: number | null;
  disabled?: boolean;
}> = ({ modelKey, label, bytes, disabled }) => {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);

  const onConfirm = async () => {
    setConfirming(false);
    setBusy(true);
    try {
      const res = await removeModelFromList(modelKey);
      addToast({
        type: 'success',
        message: res.stopped_llama_server
          ? `${label} deleted (its local model server was stopped first).`
          : `${label} deleted.`,
      });
    } catch (e) {
      addToast({
        type: 'error',
        message: `Could not delete ${label}: ${e instanceof Error ? e.message : 'request failed'}`,
        duration: 8000,
      });
    } finally {
      await refreshModelLists(queryClient);
      setBusy(false);
    }
  };

  return (
    <>
      <button
        className="btn btn-sm"
        type="button"
        disabled={disabled || busy}
        style={{ color: 'var(--accent-record)' }}
        onClick={() => setConfirming(true)}
      >
        {busy ? 'Deleting…' : 'Delete'}
      </button>
      {confirming && (
        <ConfirmDialog
          title={`Delete ${label}?`}
          message={
            <>
              This deletes the model file{bytes ? ` (${humanSize(bytes)})` : ''} and removes the
              model from your models and the chat model picker. If the model is loaded, its local
              model server stops first. You can download it again from Discover.
            </>
          }
          confirmLabel="Delete"
          danger
          onConfirm={() => void onConfirm()}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  );
};
