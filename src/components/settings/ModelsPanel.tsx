import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  cancelModelDownload,
  deleteManagedModel,
  fetchModelsCatalog,
  fetchModelsStatus,
  startModelDownload,
} from '@/api/models';
import type { ManagedModel, ManagedModelGroup } from '@/api/models';
import { useUIStore } from '@/stores/uiStore';
import { humanSize } from '@/utils/humanSize';
import { ConfirmDialog } from './ConfirmDialog';

const GROUPS: { id: ManagedModelGroup; label: string; hint: string }[] = [
  {
    id: 'transcription',
    label: 'Transcription',
    hint: 'Speech-to-text engines and the speaker-identification encoder.',
  },
  {
    id: 'indexing',
    label: 'Indexing',
    hint: 'On-device embedding, reranking, and entity extraction for the workspace index.',
  },
  {
    id: 'local-chat',
    label: 'Local chat',
    hint: 'On-device chat model weights (GGUF), served by the local model server.',
  },
];

const chipStyle = (fg: string, bg: string): React.CSSProperties => ({
  fontSize: 11,
  fontWeight: 600,
  padding: '2px 8px',
  borderRadius: 999,
  whiteSpace: 'nowrap',
  color: fg,
  background: bg,
});

const StatusChip: React.FC<{ model: ManagedModel }> = ({ model }) => {
  if (model.state === 'downloading') {
    const pct =
      model.size_bytes_estimate && model.size_bytes_estimate > 0
        ? Math.min(99, Math.floor((model.bytes_on_disk / model.size_bytes_estimate) * 100))
        : null;
    return (
      <span style={chipStyle('var(--accent)', 'var(--accent-dim, rgba(99,102,241,0.15))')}>
        Downloading {pct !== null ? `${pct}%` : humanSize(model.bytes_on_disk)}
      </span>
    );
  }
  if (model.state === 'installed') {
    return (
      <span style={chipStyle('var(--accent-live, #34c77b)', 'rgba(52,199,123,0.14)')}>
        Installed
      </span>
    );
  }
  if (model.state === 'error') {
    return (
      <span
        style={chipStyle('var(--accent-record, #e5484d)', 'rgba(229,72,77,0.12)')}
        title={model.error ?? undefined}
      >
        Error
      </span>
    );
  }
  return (
    <span style={chipStyle('var(--text-secondary, #888)', 'rgba(128,128,128,0.12)')}>
      Not downloaded
    </span>
  );
};

/**
 * Settings > Models: browse every downloadable model, download with progress,
 * cancel (the backend runs downloads in a killable worker process, so cancel
 * is real), and delete. Server state lives in react-query — no store (zustand
 * v5 selectors returning fresh objects crash at runtime). While any download
 * runs, a status query polls /api/models/status every second; react-query
 * stops interval refetches when the tab is hidden and the query unmounts with
 * the panel, so idle/hidden means no polling.
 */
export const ModelsPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [confirmDelete, setConfirmDelete] = useState<ManagedModel | null>(null);
  const addToast = useUIStore((s) => s.addToast);

  const catalogQuery = useQuery({
    queryKey: ['models-manager-catalog'],
    queryFn: fetchModelsCatalog,
  });
  const catalogDownloading = (catalogQuery.data?.models ?? []).some(
    (m) => m.state === 'downloading',
  );

  const statusQuery = useQuery({
    queryKey: ['models-manager-status'],
    queryFn: fetchModelsStatus,
    enabled: catalogDownloading,
    // Poll at 1s while its own payload still shows a download; the interval
    // stops itself the moment everything is idle, even before the catalog
    // refetch below lands.
    refetchInterval: (query) => {
      const status = query.state.data?.models;
      const stillDownloading = status
        ? Object.values(status).some((s) => s.state === 'downloading')
        : true;
      return stillDownloading ? 1000 : false;
    },
  });

  // When the poll says every download reached a terminal state, resync the
  // catalog once so `enabled` above flips off and installed/error rows show
  // authoritative catalog data.
  const statusIdle =
    !!statusQuery.data &&
    !Object.values(statusQuery.data.models).some((s) => s.state === 'downloading');
  useEffect(() => {
    if (catalogDownloading && statusIdle) {
      void queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
    }
  }, [catalogDownloading, statusIdle, queryClient]);

  // Per key, trust whichever source answered more recently: the 1s status
  // poll during a download, the full catalog otherwise (fresh after actions,
  // which invalidate it — stale status from a past download never wins).
  const models = useMemo(() => {
    const rows = catalogQuery.data?.models ?? [];
    const status = statusQuery.data?.models;
    if (!status || statusQuery.dataUpdatedAt <= catalogQuery.dataUpdatedAt) return rows;
    return rows.map((m) => {
      const s = status[m.key];
      return s ? { ...m, ...s, installed: s.state === 'installed' } : m;
    });
  }, [
    catalogQuery.data,
    statusQuery.data,
    catalogQuery.dataUpdatedAt,
    statusQuery.dataUpdatedAt,
  ]);

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
    await queryClient.invalidateQueries({ queryKey: ['models-manager-status'] });
  }, [queryClient]);

  const run = useCallback(
    async (key: string, action: () => Promise<unknown>, failLabel: string) => {
      setBusy((prev) => ({ ...prev, [key]: true }));
      try {
        await action();
        await refresh();
      } catch (e) {
        addToast({
          type: 'error',
          message: `${failLabel}: ${e instanceof Error ? e.message : 'request failed'}`,
          duration: 8000,
        });
      } finally {
        setBusy((prev) => {
          const { [key]: _drop, ...rest } = prev;
          return rest;
        });
      }
    },
    [addToast, refresh],
  );

  const onDownload = (m: ManagedModel) =>
    void run(m.key, () => startModelDownload(m.key), `Could not start ${m.label}`);
  const onCancel = (m: ManagedModel) =>
    void run(m.key, () => cancelModelDownload(m.key), `Could not cancel ${m.label}`);
  const onDelete = (m: ManagedModel) =>
    void run(
      m.key,
      async () => {
        const res = await deleteManagedModel(m.key);
        addToast({
          type: 'success',
          message: res.stopped_llama_server
            ? `${m.label} deleted (its local model server was stopped first).`
            : `${m.label} deleted.`,
        });
      },
      `Could not delete ${m.label}`,
    );

  return (
    <div className="settings-panel models-panel">
      <div className="settings-panel-header">
        <h3>Models</h3>
        <button className="btn btn-sm" onClick={() => void refresh()} type="button">
          Refresh
        </button>
      </div>

      <p className="settings-empty" style={{ marginBottom: 8 }}>
        Model weights are stored locally and downloaded from Hugging Face on demand. Up to two
        downloads run at a time; a cancelled download keeps its partial files and resumes on the
        next attempt.
      </p>

      {catalogQuery.error && (
        <p className="settings-empty" role="alert">
          Could not load the model catalog.
        </p>
      )}
      {catalogQuery.isLoading && <p className="settings-empty">Loading model catalog…</p>}

      {GROUPS.map((group) => {
        const rows = models.filter((m) => m.group === group.id);
        if (rows.length === 0) return null;
        return (
          <div key={group.id} style={{ marginBottom: 16 }}>
            <h4 style={{ margin: '12px 0 2px' }}>{group.label}</h4>
            <p className="settings-empty" style={{ margin: '0 0 6px' }}>
              {group.hint}
            </p>
            <div className="settings-list">
              {rows.map((m) => {
                const isBusy = !!busy[m.key];
                const partial = m.state !== 'downloading' && !m.installed && m.bytes_on_disk > 0;
                return (
                  <div key={m.key} className="settings-item">
                    <div className="settings-item-info">
                      <div
                        className="settings-item-name"
                        style={{ display: 'flex', alignItems: 'center', gap: 8 }}
                      >
                        {m.label}
                        <StatusChip model={m} />
                      </div>
                      <div className="settings-item-desc">
                        {m.repo_id} ·{' '}
                        {m.installed
                          ? `${humanSize(m.bytes_on_disk)} on disk`
                          : m.state === 'downloading'
                            ? `${humanSize(m.bytes_on_disk)} of ${humanSize(m.size_bytes_estimate)}`
                            : `${humanSize(m.size_bytes_estimate)} download`}
                        {partial ? ` · ${humanSize(m.bytes_on_disk)} partial on disk` : ''}
                      </div>
                      {m.state === 'error' && m.error && (
                        <div
                          className="settings-item-desc"
                          style={{ color: 'var(--accent-record, #e5484d)', marginTop: 2 }}
                          role="alert"
                        >
                          {m.error}
                        </div>
                      )}
                    </div>
                    <div className="settings-item-actions" style={{ display: 'flex', gap: 6 }}>
                      {m.state === 'downloading' ? (
                        <button
                          className="btn btn-sm"
                          type="button"
                          disabled={isBusy}
                          onClick={() => onCancel(m)}
                        >
                          Cancel
                        </button>
                      ) : m.installed ? (
                        <button
                          className="btn btn-sm"
                          type="button"
                          disabled={isBusy}
                          style={{ color: 'var(--accent-record, #e5484d)' }}
                          onClick={() => setConfirmDelete(m)}
                        >
                          Delete
                        </button>
                      ) : (
                        <>
                          <button
                            className="btn btn-primary btn-sm"
                            type="button"
                            disabled={isBusy}
                            onClick={() => onDownload(m)}
                          >
                            {m.state === 'error' ? 'Retry' : 'Download'}
                          </button>
                          {partial && (
                            <button
                              className="btn btn-sm"
                              type="button"
                              disabled={isBusy}
                              style={{ color: 'var(--accent-record, #e5484d)' }}
                              onClick={() => setConfirmDelete(m)}
                              title="Remove the partial files on disk"
                            >
                              Delete
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {confirmDelete && (
        <ConfirmDialog
          title={`Delete ${confirmDelete.label}?`}
          message={
            <>
              This removes {humanSize(confirmDelete.bytes_on_disk)} from disk
              {confirmDelete.group === 'local-chat'
                ? '. If this model is currently loaded, its local model server is stopped first. '
                : '. '}
              You can download it again at any time.
            </>
          }
          confirmLabel="Delete"
          danger
          onConfirm={() => {
            const m = confirmDelete;
            setConfirmDelete(null);
            onDelete(m);
          }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
};
