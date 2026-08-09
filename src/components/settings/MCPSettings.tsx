import React, { useCallback, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { get, post, put, del } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { useMcpToggle, fetchMcpServers, type MCPServerInfo } from '@/hooks/useMcpToggle';
import { useSaveToast } from '@/hooks/useSaveToast';

/** Approval-granularity fields the backend returns alongside MCPServerInfo's
 *  base shape (see server/mcp.py's mcp_servers_status) that the shared hook's
 *  type doesn't declare. Read via a local cast so useMcpToggle.ts (outside
 *  this panel) doesn't need to change for this settings-only surface. */
interface MCPServerExtra {
  url?: string;
  bearer_token_env_var?: string;
  approval_mode?: string;
  tool_overrides?: Record<string, string>;
  enabled_tools?: string[];
  disabled_tools?: string[];
}

const APPROVAL_MODES = ['auto', 'prompt', 'writes', 'approve'] as const;

interface MCPServerFormData {
  name: string;
  command: string;
  args: string;
  env: string;
  url: string;
  bearerTokenEnvVar: string;
  approvalMode: string;
  toolOverrides: string;
  enabledTools: string;
  disabledTools: string;
}

const EMPTY_FORM: MCPServerFormData = {
  name: '',
  command: '',
  args: '',
  env: '',
  url: '',
  bearerTokenEnvVar: '',
  approvalMode: 'auto',
  toolOverrides: '',
  enabledTools: '',
  disabledTools: '',
};

interface PendingElicitation {
  elicitation_id: string;
  server: string;
  session_id?: string | null;
  mode: string;
  message: string;
  requested_schema?: Record<string, unknown> | null;
  url?: string | null;
}

/** Parse a comma-separated tool-name list field. Blank -> no restriction. */
function parseToolNameList(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Copying a JSON snippet from docs/chat often converts straight quotes to
 *  curly ones (" " ' '), which are invalid JSON. Normalize them back. */
export const dequoteSmart = (s: string): string =>
  s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");

/** Parse the args text field into a string[]. args is just JSON: it must be a
 *  JSON array of strings (empty field = no args). There is no comma-split or
 *  wrap-as-one-arg fallback — invalid input is a hard error, never reinterpreted
 *  (silently wrapping a bracketed string as one arg is exactly what broke
 *  launched commands). The only leniency is normalizing curly quotes to straight
 *  ones, the copy-paste artifact that turns a valid array into invalid JSON. */
export function parseMcpArgs(raw: string): { args: string[] } | { error: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { args: [] };
  try {
    const parsed = z.array(z.string()).safeParse(JSON.parse(dequoteSmart(trimmed)));
    if (!parsed.success) {
      return { error: 'Args must be a JSON array of strings, e.g. ["--flag", "value"]' };
    }
    return { args: parsed.data };
  } catch {
    return { error: 'Args must be valid JSON: an array of strings, e.g. ["--flag", "value"]' };
  }
}

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

  // Elicitations awaiting a human answer (an MCP server asking for input
  // mid tool-call — see server/mcp.py's _build_elicitation_callback). A
  // separate query straight against the raw response (not through
  // fetchMcpServers, which only ever returns MCPServerInfo[]) so this
  // panel-only surface doesn't require changing that shared hook. Polled
  // while the panel is mounted since a pending elicitation can appear at
  // any time, independent of any user action here.
  const elicitationsQuery = useQuery({
    queryKey: ['mcp-elicitations'],
    queryFn: async () => {
      const data = await get<{ pending_elicitations?: PendingElicitation[] }>('/api/mcp/servers');
      return data.pending_elicitations ?? [];
    },
    refetchInterval: 4000,
  });
  const pendingElicitations = elicitationsQuery.data ?? [];
  const [elicitationContent, setElicitationContent] = useState<Record<string, string>>({});
  const [elicitationBusy, setElicitationBusy] = useState<string | null>(null);

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
    const extra = server as MCPServerInfo & MCPServerExtra;
    setIsAdding(false);
    setEditingServer(server.name);
    setFormData({
      name: server.name,
      command: server.command,
      args: JSON.stringify(server.args),
      env: server.env ? JSON.stringify(server.env) : '',
      url: extra.url ?? '',
      bearerTokenEnvVar: extra.bearer_token_env_var ?? '',
      approvalMode: extra.approval_mode ?? 'auto',
      toolOverrides: extra.tool_overrides && Object.keys(extra.tool_overrides).length
        ? JSON.stringify(extra.tool_overrides)
        : '',
      enabledTools: (extra.enabled_tools ?? []).join(', '),
      disabledTools: (extra.disabled_tools ?? []).join(', '),
    });
  }, []);

  const handleSave = useCallback(async () => {
    const name = formData.name.trim();
    const command = formData.command.trim();
    const url = formData.url.trim();
    // A server is either a local command or a remote url — at least one is
    // required, but neither is exclusively mandatory (a remote server has
    // no command at all).
    if (!name || !(command || url)) return;

    const parsedArgs = parseMcpArgs(formData.args);
    if ('error' in parsedArgs) {
      setError(parsedArgs.error);
      return;
    }
    const args = parsedArgs.args;

    let env: Record<string, string> | undefined;
    if (formData.env.trim()) {
      try {
        const parsed = z.record(z.string(), z.string()).safeParse(JSON.parse(dequoteSmart(formData.env)));
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

    let toolOverrides: Record<string, string> | undefined;
    if (formData.toolOverrides.trim()) {
      try {
        const parsed = z
          .record(z.string(), z.enum(APPROVAL_MODES))
          .safeParse(JSON.parse(dequoteSmart(formData.toolOverrides)));
        if (parsed.success) {
          toolOverrides = parsed.data;
        } else {
          setError('Tool overrides must be a JSON object mapping tool name -> auto|prompt|writes|approve');
          return;
        }
      } catch {
        setError('Tool overrides is not valid JSON');
        return;
      }
    }

    const bearerTokenEnvVar = formData.bearerTokenEnvVar.trim() || undefined;
    const approvalMode = formData.approvalMode || 'auto';
    const enabledTools = parseToolNameList(formData.enabledTools);
    const disabledTools = parseToolNameList(formData.disabledTools);

    const extraBody = {
      url: url || undefined,
      bearer_token_env_var: bearerTokenEnvVar,
      approval_mode: approvalMode,
      tool_overrides: toolOverrides,
      enabled_tools: enabledTools.length ? enabledTools : undefined,
      disabled_tools: disabledTools.length ? disabledTools : undefined,
    };

    setSaving(true);
    setError(null);
    const wasAdding = isAdding;
    await saveToast(async () => {
      if (isAdding) {
        await post('/api/mcp/servers', { name, command, args, env, ...extraBody });
      } else if (editingServer) {
        // Include new_name so Edit can also rename (no separate Rename needed).
        const body: Record<string, unknown> = { command, args, env, ...extraBody };
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

  // Answer a pending MCP elicitation via the same generic hub every other
  // approval flows through — POST /api/approval/execute with
  // {action: "mcp_elicit_respond", payload}. "accept" parses the free-form
  // JSON textarea as the form content; decline/cancel need none.
  const handleElicitationRespond = useCallback(
    async (e: PendingElicitation, responseAction: 'accept' | 'decline' | 'cancel') => {
      setElicitationBusy(e.elicitation_id);
      let content: unknown;
      if (responseAction === 'accept' && e.mode !== 'url') {
        const raw = (elicitationContent[e.elicitation_id] ?? '{}').trim();
        try {
          content = raw ? JSON.parse(raw) : {};
        } catch {
          setError('Elicitation response content is not valid JSON');
          setElicitationBusy(null);
          return;
        }
      }
      try {
        await post('/api/approval/execute', {
          action: 'mcp_elicit_respond',
          payload: { elicitation_id: e.elicitation_id, response_action: responseAction, content },
        });
        void queryClient.invalidateQueries({ queryKey: ['mcp-elicitations'] });
      } finally {
        setElicitationBusy(null);
      }
    },
    [elicitationContent, queryClient],
  );

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
            <label>Server URL (remote, instead of Command)</label>
            <input
              className="settings-input"
              placeholder="e.g. https://example.com/mcp"
              value={formData.url}
              onChange={handleFieldChange('url')}
            />
            <label>Bearer token env var (remote only)</label>
            <input
              className="settings-input"
              placeholder="e.g. MY_SERVER_TOKEN — the env var NAME, never the token itself"
              value={formData.bearerTokenEnvVar}
              onChange={handleFieldChange('bearerTokenEnvVar')}
            />
            <label>Approval mode</label>
            <select
              className="settings-input"
              value={formData.approvalMode}
              onChange={(e) => setFormData((prev) => ({ ...prev, approvalMode: e.target.value }))}
            >
              <option value="auto">auto — never ask</option>
              <option value="prompt">prompt — always ask</option>
              <option value="writes">writes — ask only for tools that look like mutations</option>
              <option value="approve">approve — always ask, no bypass</option>
            </select>
            <label>Tool overrides (JSON: tool name → mode)</label>
            <input
              className="settings-input"
              placeholder='e.g. {"delete_all": "approve"}'
              value={formData.toolOverrides}
              onChange={handleFieldChange('toolOverrides')}
            />
            <label>Enabled tools (comma-separated, blank = no restriction)</label>
            <input
              className="settings-input"
              placeholder="e.g. get_data, search_items"
              value={formData.enabledTools}
              onChange={handleFieldChange('enabledTools')}
            />
            <label>Disabled tools (comma-separated)</label>
            <input
              className="settings-input"
              placeholder="e.g. delete_all"
              value={formData.disabledTools}
              onChange={handleFieldChange('disabledTools')}
            />
          </div>
        </div>
      )}

      {pendingElicitations.length > 0 && (
        <div className="settings-list" id="mcpElicitations">
          {pendingElicitations.map((e) => (
            <div key={e.elicitation_id} className="settings-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '6px' }}>
              <div className="settings-item-info">
                <div className="settings-item-name">{e.server} needs input</div>
                <div className="settings-item-desc">{e.message}</div>
              </div>
              {e.mode === 'url' && e.url ? (
                <a href={e.url} target="_blank" rel="noreferrer">
                  {e.url}
                </a>
              ) : (
                <textarea
                  className="settings-input"
                  placeholder="{}"
                  rows={2}
                  value={elicitationContent[e.elicitation_id] ?? ''}
                  onChange={(ev) =>
                    setElicitationContent((prev) => ({ ...prev, [e.elicitation_id]: ev.target.value }))
                  }
                />
              )}
              <div className="settings-item-actions">
                <button
                  className="btn btn-primary btn-sm"
                  type="button"
                  disabled={elicitationBusy === e.elicitation_id}
                  onClick={() => void handleElicitationRespond(e, 'accept')}
                >
                  Accept
                </button>
                <button
                  className="btn btn-sm"
                  type="button"
                  disabled={elicitationBusy === e.elicitation_id}
                  onClick={() => void handleElicitationRespond(e, 'decline')}
                >
                  Decline
                </button>
                <button
                  className="btn btn-sm"
                  type="button"
                  disabled={elicitationBusy === e.elicitation_id}
                  onClick={() => void handleElicitationRespond(e, 'cancel')}
                >
                  Cancel
                </button>
              </div>
            </div>
          ))}
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
                className="toggle-switch"
                title={server.enabled
                  ? 'Disable: stop this server and remove its tools from the model for the next message.'
                  : 'Enable: start this server; its tools are available to the model on the next message.'}
              >
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={`${server.enabled ? 'Disable' : 'Enable'} MCP server ${server.name}`}
                  checked={server.enabled}
                  onChange={(e) => void handleToggleEnabled(server.name, e.target.checked)}
                />
                <span className="toggle-slider"></span>
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
