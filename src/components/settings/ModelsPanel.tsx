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
import { ChatModelVisibility } from './ChatModelVisibility';
import { ConfigJsonLink } from './ConfigEditorDialog';
import { ConfirmDialog } from './ConfirmDialog';
import { ModelBrowser } from './ModelBrowser';

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

/** 1 → "1st", 2 → "2nd", 3 → "3rd", 4 → "4th" (11–13 stay "th"). */
const ordinal = (n: number): string => {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  const suffix = { 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] ?? 'th';
  return `${n}${suffix}`;
};

/** A download that is running or waiting for a slot — rows that poll + Cancel. */
const isActive = (state: ManagedModel['state']) => state === 'downloading' || state === 'queued';

const StatusChip: React.FC<{ model: ManagedModel }> = ({ model }) => {
  if (model.state === 'queued') {
    return (
      <span style={chipStyle('var(--text-secondary, #888)', 'rgba(128,128,128,0.12)')}>
        Queued{model.queue_position != null ? ` (${ordinal(model.queue_position)})` : ''}
      </span>
    );
  }
  if (model.state === 'downloading') {
    // Server-computed fraction (bytes/estimate, capped, monotonic) — the same
    // number the chat and recording banners render. Never recomputed here.
    const pct = model.progress !== null ? Math.round(model.progress * 100) : null;
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
  // Poll while anything is downloading OR queued: the server pumps its queue
  // on status polls, so polling is what makes a queued model start at all.
  const catalogActive = (catalogQuery.data?.models ?? []).some((m) => isActive(m.state));

  const statusQuery = useQuery({
    queryKey: ['models-manager-status'],
    queryFn: fetchModelsStatus,
    enabled: catalogActive,
    // Poll at 1s while its own payload still shows a running or queued
    // download; the interval stops itself the moment everything is idle,
    // even before the catalog refetch below lands.
    refetchInterval: (query) => {
      const status = query.state.data?.models;
      const stillActive = status
        ? Object.values(status).some((s) => isActive(s.state))
        : true;
      return stillActive ? 1000 : false;
    },
  });

  // When the poll says every download reached a terminal state, resync the
  // catalog once so `enabled` above flips off and installed/error rows show
  // authoritative catalog data.
  const statusIdle =
    !!statusQuery.data && !Object.values(statusQuery.data.models).some((s) => isActive(s.state));
  useEffect(() => {
    if (catalogActive && statusIdle) {
      void queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
    }
  }, [catalogActive, statusIdle, queryClient]);

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
    await queryClient.invalidateQueries({ queryKey: ['models-disabled'] });
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

  const downloadingCount = models.filter((m) => m.state === 'downloading').length;

  const onDownload = (m: ManagedModel) =>
    void run(
      m.key,
      async () => {
        const res = await startModelDownload(m.key);
        if (res.queued) {
          // Queueing is not an error — a quick heads-up is all it needs.
          const ahead = downloadingCount + Math.max((res.queue_position ?? 1) - 1, 0);
          addToast({
            type: 'info',
            message:
              ahead > 0
                ? `${m.label} queued behind ${ahead} download${ahead === 1 ? '' : 's'}`
                : `${m.label} queued`,
            persist: false,
          });
        }
      },
      `Could not start ${m.label}`,
    );
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
        downloads run at a time; further requests queue up and start automatically. A cancelled
        download keeps its partial files and resumes on the next attempt.
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
              {group.id === 'local-chat' && (
                <>
                  {' '}
                  Add models with Discover below, or by editing <ConfigJsonLink />.
                </>
              )}
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
                            : m.state === 'queued'
                              ? `${humanSize(m.size_bytes_estimate)} download · starts when a slot frees`
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
                      {isActive(m.state) ? (
                        // Cancel works on queued rows too: the backend just
                        // dequeues them (no worker ran, partials untouched).
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
                        // Not active and not installed: Download/Retry, always.
                        // A plain not-downloaded model with partial bytes keeps
                        // JUST Download — the partials are hf resume state the
                        // next attempt picks up. But a FAILED download also
                        // offers Delete: it lets the user clear a dead job's
                        // partials and recover a model this bug once stranded.
                        <>
                          <button
                            className="btn btn-primary btn-sm"
                            type="button"
                            disabled={isBusy}
                            onClick={() => onDownload(m)}
                          >
                            {m.state === 'error' ? 'Retry' : 'Download'}
                          </button>
                          {m.state === 'error' && m.bytes_on_disk > 0 && (
                            <button
                              className="btn btn-sm"
                              type="button"
                              disabled={isBusy}
                              style={{ color: 'var(--accent-record, #e5484d)' }}
                              onClick={() => setConfirmDelete(m)}
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

      <ModelBrowser />

      <ChatModelVisibility />

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
