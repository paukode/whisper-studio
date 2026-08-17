import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { listSaved, approveSaved, deleteSaved } from '@/api/workflows';
import type { SavedWorkflow } from '@/api/workflows';
import { dialogConfirm, useUIStore } from '@/stores/uiStore';
import { toError } from '@/utils/toError';

/**
 * Workflows: the saved library, nothing else.
 *
 * This panel once also listed every run (added by user request in #60, removed
 * by user request later): runs live in chat — the inline card that launched one
 * shows its progress and result, and approval is decided there by permission
 * mode. Settings only holds what outlives a session: which saved scripts are
 * trusted to run without a card, and deleting them.
 */
export const WorkflowsPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ['workflows-saved'],
    queryFn: listSaved,
    staleTime: 15_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['workflows-saved'] });

  const handleTrust = async (name: string) => {
    try {
      await approveSaved(name);
      useUIStore.getState().addToast({
        type: 'success',
        message: `"${name}" is now trusted`,
        duration: 2500,
      });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
    await refresh();
  };

  const handleDelete = async (name: string) => {
    const ok = await dialogConfirm({
      title: `Delete workflow "${name}"?`,
      message: 'The saved script is removed. Past runs and their results are kept.',
      danger: true,
      confirmText: 'Delete',
    });
    if (!ok) return;
    try {
      await deleteSaved(name);
      useUIStore.getState().addToast({
        type: 'success',
        message: `Deleted "${name}"`,
        duration: 2500,
      });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
    await refresh();
  };

  if (isLoading) {
    return (
      <div className="settings-form" style={{ maxWidth: 720 }}>
        <p className="settings-hint">Loading saved workflows…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="settings-form" style={{ maxWidth: 720 }}>
        <p className="settings-empty" role="alert">
          Could not load saved workflows. Backend may be unavailable.
        </p>
      </div>
    );
  }

  const saved = data?.saved ?? [];

  return (
    <div className="settings-form" style={{ maxWidth: 720 }}>
      <h3 style={{ marginBottom: 8 }}>Saved workflows</h3>
      <p className="settings-hint">
        Reusable multi-agent workflow scripts saved by the assistant
        (<code>workflow_save</code>). Running one shows an approval card in chat
        the first time each session, depending on your permission mode; a{' '}
        <strong>trusted</strong> one always runs immediately and can also be
        called from inside other workflows via <code>workflow(name)</code>.
        Trust pins the current script, so any edit needs re-trusting. Running
        workflows report progress on their card in the chat that launched them.
      </p>

      {saved.length === 0 ? (
        <div className="settings-empty">
          No saved workflows yet. Ask the assistant to save one, e.g. &ldquo;save
          this workflow as nightly-review&rdquo;.
        </div>
      ) : (
        <div className="settings-list">
          {saved.map((wf: SavedWorkflow) => (
            <div key={wf.name} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {wf.name}
                  <span
                    style={{
                      marginLeft: 8,
                      fontSize: '0.8em',
                      fontWeight: 500,
                      padding: '1px 6px',
                      borderRadius: 3,
                      background: wf.trusted ? 'var(--accent-dim)' : 'var(--bg-secondary)',
                      color: wf.trusted ? 'var(--accent)' : 'var(--text-muted)',
                    }}
                  >
                    {wf.trusted ? 'trusted' : 'untrusted'}
                  </span>
                </div>
                <div className="settings-item-desc">
                  {wf.description || 'No description.'}
                  {wf.phases.length > 0 && ` · ${wf.phases.length} phase${wf.phases.length === 1 ? '' : 's'}`}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
                {!wf.trusted && (
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    onClick={() => void handleTrust(wf.name)}
                  >
                    Trust
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => void handleDelete(wf.name)}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
