import React, { useCallback, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { post, put, del } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { useMcpToggle, fetchMcpServers, type MCPServerInfo } from '@/hooks/useMcpToggle';
import { useSaveToast } from '@/hooks/useSaveToast';

interface MCPServerFormData {
  name: string;
  command: string;
  args: string;
  env: string;
}

const EMPTY_FORM: MCPServerFormData = {
  name: '',
  command: '',
  args: '',
  env: '',
};

export const MCPSettings: React.FC = () => {
  const queryClient = useQueryClient();
  const [editingServer, setEditingServer] = useState<string | null>(null);
  const [renamingServer, setRenamingServer] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [formData, setFormData] = useState<MCPServerFormData>(EMPTY_FORM);
  const [isAdding, setIsAdding] = useState(false);
  const [error, setError] = useState<string | null>(null); // form-validation + mutation errors

  // Server list loads via react-query (no setState-in-effect). Mutations below
  // invalidate ['mcp-servers'] to refetch; the enable toggle updates the cache
  // optimistically via setQueryData.
  const serversQuery = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: fetchMcpServers,
    staleTime: 30_000,
  });
  // Stable identity when the query data is unchanged, so callbacks that depend
  // on `servers` (e.g. rename) don't get a new reference every render.
  const servers = useMemo(() => serversQuery.data ?? [], [serversQuery.data]);
  const displayError = error ?? (serversQuery.isError ? 'Failed to load MCP servers from backend.' : null);
  const [saving, setSaving] = useState(false);

  // Shared persistent toggle — updates this panel's cache AND the toolbar's
  // store copy + PATCHes, so the change is live in both places.
  const handleToggleEnabled = useMcpToggle();

  // Toast feedback for save/delete/restart/rename (the editor closes / the row
  // refetches, so an inline indicator would unmount before it's seen).
  const saveToast = useSaveToast();

  const handleFieldChange = useCallback(
    (field: keyof MCPServerFormData) =>
      (e: React.ChangeEvent<HTMLInputElement>) => {
        setFormData((prev) => ({ ...prev, [field]: e.target.value }));
      },
    [],
  );

  const handleAdd = useCallback(() => {
    setIsAdding(true);
    setEditingServer(null);
    setFormData(EMPTY_FORM);
  }, []);

  const handleEdit = useCallback((server: MCPServerInfo) => {
    setIsAdding(false);
    setEditingServer(server.name);
    setFormData({
      name: server.name,
      command: server.command,
      args: JSON.stringify(server.args),
      env: server.env ? JSON.stringify(server.env) : '',
    });
  }, []);

  const handleSave = useCallback(async () => {
    const name = formData.name.trim();
    const command = formData.command.trim();
    if (!name || !command) return;

    let args: string[] = [];
    if (formData.args.trim()) {
      try {
        const parsed = z.array(z.string()).safeParse(JSON.parse(formData.args));
        if (parsed.success) {
          args = parsed.data;
        } else {
          setError('Args must be a JSON array of strings, e.g. ["--flag", "value"]');
          return;
        }
      } catch {
        args = formData.args.split(',').map((a) => a.trim()).filter(Boolean);
      }
    }

    let env: Record<string, string> | undefined;
    if (formData.env.trim()) {
      try {
        const parsed = z.record(z.string(), z.string()).safeParse(JSON.parse(formData.env));
        if (parsed.success) {
          env = parsed.data;
        } else {
          setError('Env must be a JSON object of string key-value pairs, e.g. {"KEY": "value"}');
          return;
        }
      } catch {
        setError('Env is not valid JSON');
        return;
      }
    }

    setSaving(true);
    setError(null);
    const wasAdding = isAdding;
    await saveToast(async () => {
      if (isAdding) {
        await post('/api/mcp/servers', { name, command, args, env });
      } else if (editingServer) {
        // Include new_name so Edit can also rename (no separate Rename needed).
        const body: Record<string, unknown> = { command, args, env };
        if (name !== editingServer) body.new_name = name;
        await put(`/api/mcp/servers/${encodeURIComponent(editingServer)}`, body);
      }
      setIsAdding(false);
      setEditingServer(null);
      setFormData(EMPTY_FORM);
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      // Refresh the toolbar's store copy so a new/renamed server shows there too.
      await useSettingsStore.getState().loadMCP();
      await useSettingsStore.getState().loadSkills();
    }, { success: wasAdding ? 'MCP server added' : 'MCP server saved', error: 'Failed to save MCP server' });
    setSaving(false);
  }, [formData, isAdding, editingServer, queryClient, saveToast]);

  const handleCancel = useCallback(() => {
    setIsAdding(false);
    setEditingServer(null);
    setFormData(EMPTY_FORM);
  }, []);

  const handleDelete = useCallback(async (name: string) => {
    await saveToast(async () => {
      await del(`/api/mcp/servers/${encodeURIComponent(name)}`);
      setEditingServer(null);
      setFormData(EMPTY_FORM);
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      await useSettingsStore.getState().loadMCP();
      await useSettingsStore.getState().loadSkills();
    }, { success: 'MCP server deleted', error: 'Failed to delete MCP server' });
  }, [queryClient, saveToast]);

  const handleRestart = useCallback(async (name: string) => {
    await saveToast(async () => {
      await post(`/api/mcp/servers/${encodeURIComponent(name)}/restart`);
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      await useSettingsStore.getState().loadMCP();
      await useSettingsStore.getState().loadSkills();
    }, { success: 'MCP server restarting', error: 'Failed to restart MCP server' });
  }, [queryClient, saveToast]);

  const handleRenameStart = useCallback((name: string) => {
    setRenamingServer(name);
    setRenameValue(name);
  }, []);

  const handleRenameConfirm = useCallback(async () => {
    if (!renamingServer || !renameValue.trim()) return;
    const newName = renameValue.trim();
    if (newName === renamingServer) {
      setRenamingServer(null);
      return;
    }
    await saveToast(async () => {
      // Use the unified PUT endpoint with the server's current config + new_name
      const server = servers.find((s) => s.name === renamingServer);
      await put(`/api/mcp/servers/${encodeURIComponent(renamingServer)}`, {
        new_name: newName,
        command: server?.command ?? '',
        args: server?.args ?? [],
        env: server?.env ?? {},
      });
      setRenamingServer(null);
      setRenameValue('');
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      await useSettingsStore.getState().loadMCP();
      await useSettingsStore.getState().loadSkills();
    }, { success: 'MCP server renamed', error: 'Failed to rename MCP server' });
  }, [renamingServer, renameValue, servers, queryClient, saveToast]);

  const handleRenameCancel = useCallback(() => {
    setRenamingServer(null);
    setRenameValue('');
  }, []);

  const statusClass = (status: string) => {
    if (status === 'running' || status === 'connected') return 'connected';
    if (status === 'error') return 'error';
    return 'stopped';
  };

  const isFormVisible = isAdding || editingServer !== null;

  return (
    <div className="settings-panel mcp-settings">
      <div className="settings-toolbar">
        <button
          className="btn btn-sm"
          onClick={handleAdd}
          disabled={isFormVisible}
          type="button"
        >
          + Add Server
        </button>
      </div>

      {error && <p className="settings-empty">{error}</p>}

      {isFormVisible && (
        <div className="settings-editor" id="mcpEditor">
          <div className="editor-header">
            <input
              className="editor-name-input"
              placeholder="Server name"
              value={formData.name}
              onChange={handleFieldChange('name')}
            />
            <div className="editor-actions">
              <button className="btn btn-primary btn-sm" onClick={() => void handleSave()} type="button" disabled={saving}>
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button className="btn btn-sm" onClick={handleCancel} type="button" disabled={saving}>Cancel</button>
            </div>
          </div>
          <div className="settings-form">
            <label>Command</label>
            <input
              className="settings-input"
              placeholder="e.g. npx"
              value={formData.command}
              onChange={handleFieldChange('command')}
            />
            <label>Args (JSON array)</label>
            <input
              className="settings-input"
              placeholder='e.g. ["-y", "@upstash/context7-mcp"]'
              value={formData.args}
              onChange={handleFieldChange('args')}
            />
            <label>Env (JSON object)</label>
            <input
              className="settings-input"
              placeholder='e.g. {"API_KEY": "..."}'
              value={formData.env}
              onChange={handleFieldChange('env')}
            />
          </div>
        </div>
      )}

      {servers.length === 0 && !isFormVisible && !displayError && (
        <p className="settings-empty">No MCP servers configured.</p>
      )}

      <div className="settings-list" id="mcpList">
        {[...servers].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })).map((server) => (
          <div key={server.name} className="settings-item">
            <span className={`mcp-status-dot ${statusClass(server.status)}`}></span>
            <div className="settings-item-info">
              {renamingServer === server.name ? (
                <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
                  <input
                    className="settings-input"
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void handleRenameConfirm();
                      if (e.key === 'Escape') { e.preventDefault(); handleRenameCancel(); }
                    }}
                    style={{ width: '140px', padding: '2px 6px' }}
                    autoFocus
                  />
                  <button className="btn btn-sm" onClick={() => void handleRenameConfirm()} type="button">OK</button>
                  <button className="btn btn-sm" onClick={handleRenameCancel} type="button">✕</button>
                </div>
              ) : (
                <>
                  <div className="settings-item-name">{server.name}</div>
                  <div className="settings-item-desc">{server.command} {server.args.join(' ')}</div>
                </>
              )}
            </div>
            <div className="settings-item-actions">
              <label
                style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.8em', cursor: 'pointer' }}
                title={server.enabled
                  ? 'Disable: stop advertising this server’s tools to the model. Connection stays warm.'
                  : 'Enable: advertise this server’s tools to the model. Adds ~1.5-5k tokens per request.'}
              >
                <input
                  type="checkbox"
                  checked={server.enabled}
                  onChange={(e) => void handleToggleEnabled(server.name, e.target.checked)}
                />
                <span>{server.enabled ? 'On' : 'Off'}</span>
              </label>
              <button className="btn btn-sm" onClick={() => handleEdit(server)} type="button">Edit</button>
              <button className="btn btn-sm" onClick={() => handleRenameStart(server.name)} type="button">Rename</button>
              <button className="btn btn-sm" onClick={() => void handleRestart(server.name)} type="button">Restart</button>
              <button className="btn btn-sm" style={{ color: 'var(--accent-record)' }} onClick={() => void handleDelete(server.name)} type="button">Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
