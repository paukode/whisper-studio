import React, { useCallback, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';

interface Plugin {
  name: string;
  description?: string;
  enabled?: boolean;
  /** Server-side flag for plugins that ship with the product and cannot
   *  be toggled off (e.g. `security_checks`). The UI renders the switch
   *  as checked + disabled with an explanatory tooltip. */
  protected?: boolean;
  version?: string;
  status?: string;
  error?: string | null;
  /** Tool names the plugin registered live (empty until it is loaded). */
  tools?: string[];
}

interface PluginsPayload {
  plugins: Plugin[];
  pluginsDir: string;
}

async function fetchPlugins(): Promise<PluginsPayload> {
  const data = await get<{ plugins?: Plugin[]; plugins_dir?: string } | Plugin[]>('/api/plugins');
  if (Array.isArray(data)) return { plugins: data, pluginsDir: '' };
  return { plugins: data.plugins ?? [], pluginsDir: data.plugins_dir ?? '' };
}

// A minimal, correct plugin the loader accepts. Mirrors the shape the backend
// documents in the plugins/README.md and the (tool_input, transcript,
// attachments) -> str executor contract (server/executors/__init__.py,
// server/skills.py). Uploads are validated with `ast` for a top-level
// `register`, so this is the smallest file that both passes validation and
// actually loads and runs.
const PLUGIN_TEMPLATE = `"""Example Whisper Studio plugin.

Save this as e.g. my_plugin.py and add it with "Add plugin", then enable it
below. New plugins always start disabled until you turn them on.
"""

__version__ = "1.0.0"
__description__ = "My custom plugin"


def register(app, executor_registry):
    # Called once when the plugin is enabled. Register one executor per tool.
    # The registry key is the tool name (keep it snake_case and unique).
    def my_tool(tool_input, transcript, attachments):
        # tool_input is the model's JSON args for this tool.
        # Return a string; it becomes the tool result the model reads.
        return "hello from my plugin"

    executor_registry["my_tool"] = my_tool
`;

export const PluginsPanel: React.FC = () => {
  const { data, error, refetch } = useQuery({
    queryKey: ['plugins'],
    queryFn: fetchPlugins,
    staleTime: 5 * 60_000, // install-time data; changes rarely
  });
  const plugins = data?.plugins ?? [];
  const pluginsDir = data?.pluginsDir ?? '';

  // Local overlay used for optimistic toggles so the switch feels
  // instant while the PATCH is in flight. Keyed by plugin name. On
  // success the next refetch's data flows through; on failure we drop
  // the entry to roll back to the server-reported value.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  // Per-row toggle error string. Cleared when the user toggles again.
  const [toggleError, setToggleError] = useState<Record<string, string>>({});
  const [uploading, setUploading] = useState(false);
  // Reveals the read-only plugin template so the user can see the expected
  // shape of a valid plugin .py before uploading one.
  const [showTemplate, setShowTemplate] = useState(false);
  const [templateCopied, setTemplateCopied] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const addToast = useUIStore((s) => s.addToast);

  const handleCopyTemplate = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(PLUGIN_TEMPLATE);
      setTemplateCopied(true);
      window.setTimeout(() => setTemplateCopied(false), 1500);
    } catch {
      addToast({ type: 'error', message: 'Could not copy the template to the clipboard.' });
    }
  }, [addToast]);

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
            /* not JSON, keep the HTTP code */
          }
          throw new Error(detail);
        }
        const body = (await resp.json()) as { note?: string; restartRequired?: boolean };
        await refetch();
        // Enabling loads the plugin live (its tools are available next message);
        // a plugin that also registers HTTP routes/hooks needs a restart, which
        // the server flags. Surface that honestly.
        if (body.restartRequired && body.note) {
          addToast({ type: 'warning', message: body.note });
        } else {
          addToast({ type: 'success', message: body.note || `${name} ${next ? 'enabled' : 'disabled'}` });
        }
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

  const uploadPlugin = useCallback(
    async (file: File): Promise<void> => {
      const send = async (overwrite: boolean): Promise<void> => {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('overwrite', overwrite ? 'true' : 'false');
        const resp = await fetch('/api/plugins/upload', { method: 'POST', body: fd });
        if (resp.status === 409 && !overwrite) {
          const body = (await resp.json().catch(() => ({}))) as { detail?: string };
          if (window.confirm(`${body.detail ?? 'That plugin already exists.'} Replace it?`)) {
            return send(true);
          }
          return;
        }
        if (!resp.ok) {
          const body = (await resp.json().catch(() => ({}))) as { detail?: string };
          addToast({ type: 'error', message: body.detail ?? `Upload failed (HTTP ${resp.status})` });
          return;
        }
        const body = (await resp.json()) as { name?: string };
        addToast({
          type: 'success',
          message: `${body.name ?? 'Plugin'} added and left off. Enable it below to load it.`,
        });
        await refetch();
      };
      return send(false);
    },
    [addToast, refetch],
  );

  const handleFilePicked = useCallback(
    async (file: File | undefined) => {
      if (!file) return;
      setUploading(true);
      try {
        await uploadPlugin(file);
      } catch (err) {
        addToast({
          type: 'error',
          message: err instanceof Error ? err.message : 'Upload failed',
        });
      } finally {
        setUploading(false);
      }
    },
    [uploadPlugin, addToast],
  );

  return (
    <div className="settings-panel plugins-panel">
      <div className="settings-panel-header">
        <h3>Plugins</h3>
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            className="btn btn-primary btn-sm"
            type="button"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? 'Adding…' : 'Add plugin…'}
          </button>
          <button
            className="btn btn-sm"
            type="button"
            aria-expanded={showTemplate}
            onClick={() => setShowTemplate((v) => !v)}
          >
            {showTemplate ? 'Hide template' : 'View template'}
          </button>
          <button className="btn btn-sm" onClick={() => void refetch()} type="button">
            Refresh
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept=".py"
          aria-label="Add plugin file"
          style={{ display: 'none' }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = ''; // allow re-picking the same file
            void handleFilePicked(file);
          }}
        />
      </div>

      <p className="plugins-intro">
        Plugins are opt-in. A newly added plugin stays <strong>off</strong> until you enable it.
      </p>
      <ul className="plugins-notes">
        <li>Enabling loads it live; its tools are available on your next message.</li>
        <li>Required safety plugins are locked on.</li>
        <li>
          A plugin that registers HTTP routes needs a restart for those routes (its tools are
          still live).
        </li>
      </ul>

      {showTemplate && (
        <div className="plugins-template">
          <div className="plugins-template-header">
            <span>Plugin template (my_plugin.py)</span>
            <button
              className="btn btn-sm"
              type="button"
              onClick={() => void handleCopyTemplate()}
            >
              {templateCopied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="plugins-template-code">
            <code>{PLUGIN_TEMPLATE}</code>
          </pre>
          <p className="plugins-template-hint">
            Save this as a <code>.py</code> file, add it above, then enable it. The tool name is
            the registry key; new plugins start disabled.
          </p>
        </div>
      )}

      {pluginsDir && (
        <p className="settings-empty" style={{ marginTop: 0, marginBottom: 8, fontSize: '0.8em', opacity: 0.7 }}>
          Folder: <code>{pluginsDir}</code>
        </p>
      )}

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
              ? 'Disable: unload this plugin now (its tools stop being offered).'
              : 'Enable: load this plugin now (its tools are available next message).';

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
                {isEnabled && plugin.tools && plugin.tools.length > 0 && (
                  <div className="settings-item-desc" style={{ opacity: 0.6, fontSize: '0.8em', marginTop: 2 }}>
                    Tools: {plugin.tools.join(', ')}
                  </div>
                )}
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
                  className="toggle-switch"
                  title={tooltip}
                  style={{ cursor: isProtected ? 'not-allowed' : 'pointer' }}
                >
                  <input
                    type="checkbox"
                    checked={isEnabled}
                    disabled={isProtected || pending[plugin.name] !== undefined}
                    onChange={(e) =>
                      void handleToggle(plugin.name, e.target.checked)
                    }
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
