import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

/** One row of GET/PUT /api/config/models-disabled: a chat-catalog model with
 *  its hide state. Disabled models stay in this list (or they could never be
 *  re-enabled) — they are only hidden from the composer's model picker. */
export interface ModelVisibilityRow {
  key: string;
  label: string;
  is_local: boolean;
  disabled: boolean;
}

export interface ModelsDisabledResponse {
  disabled: string[];
  models: ModelVisibilityRow[];
}

export const fetchModelsDisabled = () =>
  get<ModelsDisabledResponse>('/api/config/models-disabled');
export const saveModelsDisabled = (disabled: string[]) =>
  put<ModelsDisabledResponse & { ok: boolean }>('/api/config/models-disabled', { disabled });

/**
 * Settings > Models: per-model visibility toggles for the chat catalog.
 *
 * Lists EVERY chat model (cloud and on-device, including currently hidden
 * ones) with an on/off switch. All models are on by default; toggling one off
 * adds its key to `chat_models_disabled` in the USER config via the dedicated
 * endpoint (the raw config editor writes the same key), and the composer's
 * picker store is refreshed immediately so the model appears/disappears
 * without a reload. Hiding a model never touches its downloaded weights —
 * those stay manageable in the download list above.
 */
export const ChatModelVisibility: React.FC = () => {
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ['models-disabled'],
    queryFn: fetchModelsDisabled,
  });
  // Defensive shapes: a malformed payload must degrade to an empty list, not
  // crash the whole Models tab render.
  const rows = Array.isArray(query.data?.models) ? query.data.models : [];
  const disabled = Array.isArray(query.data?.disabled) ? query.data.disabled : [];

  const onToggle = async (row: ModelVisibilityRow) => {
    const next = row.disabled ? disabled.filter((k) => k !== row.key) : [...disabled, row.key];
    // Read the selection BEFORE the picker store refreshes and may fall back.
    const selected = useSettingsStore.getState().selectedModel;
    setBusyKey(row.key);
    try {
      const res = await saveModelsDisabled(next);
      // The response IS the post-save list; render from it rather than refetching.
      queryClient.setQueryData(['models-disabled'], {
        disabled: res.disabled,
        models: res.models,
      });
      if (!row.disabled && row.key === selected) {
        addToast({
          type: 'info',
          message: `${row.label} was your selected chat model. The picker fell back to another model.`,
          duration: 6000,
        });
      }
      // Update the composer picker live; loadModels keeps a still-valid
      // selection and falls back the same way initial selection does.
      await useSettingsStore.getState().loadModels();
    } catch (e) {
      addToast({
        type: 'error',
        message: `Could not update ${row.label}: ${e instanceof Error ? e.message : 'request failed'}`,
        duration: 8000,
      });
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div style={{ marginBottom: 16 }}>
      <h4 style={{ margin: '12px 0 2px' }}>Chat model visibility</h4>
      <p className="settings-empty" style={{ margin: '0 0 6px' }}>
        Every chat model in your catalog, cloud and on-device. Toggle one off to hide it from the
        chat model picker; it stays listed here so you can re-enable it, and any downloaded weights
        stay manageable above. All models are visible by default.
      </p>
      {query.isError && (
        <p className="settings-empty" role="alert">
          Could not load the chat model list.
        </p>
      )}
      {query.isLoading && <p className="settings-empty">Loading chat models…</p>}
      <div className="settings-list">
        {rows.map((m) => (
          <div key={m.key} className="settings-item" data-testid={`model-visibility-${m.key}`}>
            <div className="settings-item-info">
              <div
                className="settings-item-name"
                style={{ display: 'flex', alignItems: 'center', gap: 8 }}
              >
                {m.label}
                {m.is_local && <span className="model-local-badge">Local</span>}
                {m.disabled && (
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      padding: '2px 8px',
                      borderRadius: 999,
                      whiteSpace: 'nowrap',
                      color: 'var(--text-secondary, #888)',
                      background: 'rgba(128,128,128,0.12)',
                    }}
                  >
                    Hidden from picker
                  </span>
                )}
              </div>
            </div>
            <div className="settings-item-actions">
              <label
                className="toggle-switch"
                title={m.disabled ? 'Hidden from the model picker' : 'Visible in the model picker'}
              >
                <input
                  type="checkbox"
                  checked={!m.disabled}
                  disabled={busyKey === m.key}
                  onChange={() => void onToggle(m)}
                  aria-label={`Toggle ${m.label} in the model picker`}
                />
                <span className="toggle-slider"></span>
              </label>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
