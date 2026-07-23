import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';

interface Plugin {
  name: string;
  description?: string;
  enabled?: boolean;
  /** Server-side flag for plugins that ship with the product and cannot
   *  be toggled off (e.g. `security_checks`). The UI renders the
   *  checkbox as checked + disabled with an explanatory tooltip. */
  protected?: boolean;
  version?: string;
  status?: string;
  error?: string | null;
}

async function fetchPlugins(): Promise<Plugin[]> {
  const data = await get<{ plugins: Plugin[] } | Plugin[]>('/api/plugins');
  return Array.isArray(data) ? data : (data.plugins ?? []);
}

export const PluginsPanel: React.FC = () => {
  const { data: plugins = [], error, refetch } = useQuery({
    queryKey: ['plugins'],
    queryFn: fetchPlugins,
    staleTime: 5 * 60_000,  // install-time data; changes rarely
  });

  // Local overlay used for optimistic toggles so the checkbox feels
  // instant while the PATCH is in flight. Keyed by plugin name. On
  // success the next refetch's data flows through; on failure we drop
  // the entry to roll back to the server-reported value.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  // Per-row toggle error string. Cleared when the user toggles again.
  const [toggleError, setToggleError] = useState<Record<string, string>>({});
  const addToast = useUIStore((s) => s.addToast);

  const handleToggle = useCallback(
    async (name: string, next: boolean) => {
      setPending((prev) => ({ ...prev, [name]: next }));
      setToggleError((prev) => {
        const { [name]: _drop, ...rest } = prev;
        return rest;
      });
      try {
        const resp = await fetch(
          `/api/plugins/${encodeURIComponent(name)}/toggle`,
          { method: 'PATCH' },
        );
        if (!resp.ok) {
          // Pull out the FastAPI `detail` so the protected-plugin
          // message surfaces verbatim instead of a generic "HTTP 409".
          let detail = `HTTP ${resp.status}`;
          try {
            const body = (await resp.json()) as { detail?: string };
            if (body.detail) detail = body.detail;
          } catch {
            /* not JSON — keep the HTTP code */
          }
          throw new Error(detail);
        }
        await refetch();
        addToast({ type: 'success', message: `${name} ${next ? 'enabled' : 'disabled'}` });
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Toggle failed';
        setToggleError((prev) => ({ ...prev, [name]: msg }));
      } finally {
        setPending((prev) => {
          const { [name]: _drop, ...rest } = prev;
          return rest;
        });
      }
    },
    [refetch, addToast],
  );

  return (
    <div className="settings-panel plugins-panel">
      <div className="settings-panel-header">
        <h3>Plugins</h3>
        <button className="btn btn-sm" onClick={() => void refetch()} type="button">
          Refresh
        </button>
      </div>

      <p className="settings-empty" style={{ marginBottom: 8 }}>
        Plugins are opt-in. New plugins dropped into <code>plugins/</code> stay off
        until toggled on. Required safety plugins are locked on. Restart the server
        to apply toggle changes.
      </p>

      {error && (
        <p className="settings-empty" role="alert">
          Could not load plugins. The plugins API may not be available.
        </p>
      )}

      {plugins.length === 0 && !error && (
        <p className="settings-empty">No plugins installed.</p>
      )}

      <div className="settings-list">
        {plugins.map((plugin) => {
          // Render with the optimistic pending value if a toggle is in
          // flight; otherwise use the server-reported `enabled`.
          const isEnabled =
            pending[plugin.name] !== undefined
              ? pending[plugin.name]
              : !!plugin.enabled;
          const isProtected = !!plugin.protected;
          const rowError = toggleError[plugin.name];
          const tooltip = isProtected
            ? 'Required for safety: this plugin cannot be disabled.'
            : isEnabled
              ? 'Disable: stop loading this plugin on next server start.'
              : 'Enable: load this plugin on next server start.';

          return (
            <div key={plugin.name} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {plugin.name}
                  {plugin.version ? (
                    <span style={{ opacity: 0.5, marginLeft: 6, fontSize: '0.85em' }}>
                      v{plugin.version}
                    </span>
                  ) : null}
                  {isProtected && (
                    <span
                      style={{
                        marginLeft: 6,
                        fontSize: '0.7em',
                        padding: '1px 6px',
                        borderRadius: 4,
                        background: 'var(--accent)',
                        color: '#fff',
                        fontWeight: 600,
                        letterSpacing: 0.3,
                      }}
                      title="Locked on for safety"
                    >
                      REQUIRED
                    </span>
                  )}
                </div>
                <div className="settings-item-desc">{plugin.description ?? ''}</div>
                {rowError && (
                  <div
                    className="settings-item-desc"
                    style={{ color: 'var(--accent-record, #ef4444)', marginTop: 2 }}
                    role="alert"
                  >
                    {rowError}
                  </div>
                )}
              </div>
              <div className="settings-item-actions">
                <label
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    fontSize: '0.85em',
                    cursor: isProtected ? 'not-allowed' : 'pointer',
                    opacity: isProtected ? 0.75 : 1,
                  }}
                  title={tooltip}
                >
                  <input
                    type="checkbox"
                    checked={isEnabled}
                    disabled={isProtected || pending[plugin.name] !== undefined}
                    onChange={(e) =>
                      void handleToggle(plugin.name, e.target.checked)
                    }
                  />
                  <span>{isEnabled ? 'On' : 'Off'}</span>
                </label>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
