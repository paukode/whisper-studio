import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSaveToast } from '@/hooks/useSaveToast';

interface FlagState {
  enabled: boolean;
  default: boolean;
  description: string;
  category: string;
  source: 'config' | 'default';
}

type FlagMap = Record<string, FlagState>;

export const FeatureFlagsPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const saveToast = useSaveToast();

  const flagsQuery = useQuery({
    queryKey: ['feature-flags'],
    queryFn: () => get<FlagMap>('/api/feature-flags'),
    staleTime: 5 * 60_000,
  });
  const flags = flagsQuery.data ?? {};

  const toggle = async (name: string, current: boolean) => {
    const next = !current;
    await saveToast(async () => {
      // auto_memory is also mirrored in the toolbar — route it through the store
      // setter so both stay in sync; other flags persist directly.
      if (name === 'auto_memory') {
        useSettingsStore.getState().setAutoMemory(next);
      } else {
        await put(`/api/feature-flags/${name}`, { enabled: next });
      }
      void queryClient.invalidateQueries({ queryKey: ['feature-flags'] });
    }, { success: `${name} ${next ? 'enabled' : 'disabled'}`, error: `Failed to update ${name}` });
  };

  const names = Object.keys(flags).sort((a, b) => {
    const ca = flags[a].category, cb = flags[b].category;
    return ca === cb ? a.localeCompare(b) : ca.localeCompare(cb);
  });

  const displayError = flagsQuery.isError ? 'Could not load feature flags.' : null;

  return (
    <div className="settings-form" style={{ maxWidth: 600 }}>
      <p className="settings-hint">
        Toggle features without editing config.json. Changes persist immediately.
      </p>
      {displayError && <p className="settings-empty">{displayError}</p>}

      <div className="settings-list">
        {names.length === 0 && !displayError && <p className="settings-empty">No feature flags.</p>}
        {names.map((name) => {
          const f = flags[name];
          return (
            <div key={name} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {name}
                  <span className="model-local-badge" style={{ textTransform: 'none' }}>{f.category}</span>
                  {f.source === 'config' && f.enabled !== f.default && (
                    <span className="model-retention-badge" style={{ textTransform: 'none' }}>overridden</span>
                  )}
                </div>
                <div className="settings-item-desc">{f.description}</div>
              </div>
              <div className="settings-item-actions">
                <label className="toggle-switch" title={f.enabled ? 'On' : 'Off'}>
                  <input
                    type="checkbox"
                    checked={f.enabled}
                    onChange={() => void toggle(name, f.enabled)}
                    aria-label={`Toggle ${name}`}
                  />
                  <span className="toggle-slider"></span>
                </label>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
