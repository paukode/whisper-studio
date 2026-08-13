import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { listSaved, listRuns, approveSaved, deleteSaved } from '@/api/workflows';
import type { SavedWorkflow, WorkflowRun } from '@/api/workflows';
import { dialogConfirm, useUIStore } from '@/stores/uiStore';
import { toError } from '@/utils/toError';

/**
 * Workflows: what has RUN, and what is saved to run again.
 *
 * The runs list exists because a launched workflow used to be visible only as
 * the inline card in the chat that started it — this panel showed saved scripts
 * alone, so a user who had run several workflows still saw "no workflows".
 *
 * Saved workflows land here via the assistant's `workflow_save` tool and are
 * untrusted by default: running one by name shows an approval preview card
 * each time, and nested `workflow(name)` calls from other scripts refuse to
 * load it. Trusting a workflow records its current script hash, so both paths
 * run it without a preview until the script changes.
 */
export const WorkflowsPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ['workflows-saved'],
    queryFn: listSaved,
    staleTime: 15_000,
  });
  // Runs refresh faster than saved scripts and keep polling while one is live,
  // so a swarm launched from chat shows up here without a manual reload.
  const { data: runsData } = useQuery({
    queryKey: ['workflows-runs'],
    queryFn: () => listRuns(),
    staleTime: 5_000,
    refetchInterval: (q) =>
      (q.state.data?.runs ?? []).some((r: WorkflowRun) => r.status === 'running') ? 5_000 : false,
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
  const runs = runsData?.runs ?? [];

  return (
    <div className="settings-form" style={{ maxWidth: 720 }}>
      <h3 style={{ marginBottom: 8 }}>Runs</h3>
      <p className="settings-hint">
        Every workflow this app has run, newest first. A run keeps going in the
        background after the turn that launched it ends — you can carry on
        chatting, and its result is reported back when it finishes.
      </p>

      {runs.length === 0 ? (
        <div className="settings-empty">
          No workflow runs yet. Switch the composer to <strong>Ultracode</strong> and
          ask for substantial work — the assistant writes a workflow script and
          runs it as a background agent swarm.
        </div>
      ) : (
        <div className="settings-list">
          {runs.map((run: WorkflowRun) => (
            <div key={run.run_id} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {run.name || run.run_id}
                  <span
                    style={{
                      marginLeft: 8,
                      fontSize: '0.8em',
                      fontWeight: 500,
                      padding: '1px 6px',
                      borderRadius: 3,
                      background: run.status === 'running' ? 'var(--accent-dim)' : 'var(--bg-secondary)',
                      color: run.status === 'running' ? 'var(--accent)' : 'var(--text-muted)',
                    }}
                  >
                    {run.status}
                  </span>
                </div>
                <div className="settings-item-desc">
                  {run.agents_spawned} agent{run.agents_spawned === 1 ? '' : 's'}
                  {' · '}${run.cost_usd.toFixed(2)}
                  {run.cap_reached && ' · budget cap reached'}
                  {run.error && ` · ${run.error}`}
                  {run.started_at && ` · ${new Date(run.started_at).toLocaleString()}`}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <h3 style={{ marginTop: 24, marginBottom: 8 }}>Saved</h3>
      <p className="settings-hint">
        Reusable multi-agent workflow scripts saved by the assistant
        (<code>workflow_save</code>). An untrusted workflow shows an approval
        preview card every time it runs by name; a <strong>trusted</strong> one
        runs immediately and can also be called from inside other workflows
        via <code>workflow(name)</code>. Trust pins the current script, so any
        edit needs re-trusting.
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
