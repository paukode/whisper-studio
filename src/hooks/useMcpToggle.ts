import { useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useSettingsStore } from '@/stores/settingsStore';
import { get } from '@/api/client';

/** Full MCP server record as returned by GET /api/mcp/servers and cached under
 *  the ['mcp-servers'] query key (used by the Settings panel). */
export interface MCPServerInfo {
  name: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
  enabled: boolean;
  status: string;
  error?: string | null;
}

/** Canonical fetcher for the ['mcp-servers'] query key. Every consumer (MCP
 *  settings, the API-keys AgentCore toggle, the toggle hook's optimistic
 *  cache writes) must use this so the cache is ALWAYS `MCPServerInfo[]` — a
 *  query that returned the raw `{servers: {...}}` object instead poisoned the
 *  shared cache and made `[...servers]` throw "servers is not iterable". */
export async function fetchMcpServers(): Promise<MCPServerInfo[]> {
  const data = await get<{ servers?: Record<string, { command?: string; args?: string[]; env?: Record<string, string>; enabled?: boolean; status?: string; error?: string | null }> }>('/api/mcp/servers');
  return Object.entries(data.servers ?? {}).map(([name, info]) => ({
    name,
    command: info.command ?? '',
    args: info.args ?? [],
    env: info.env,
    enabled: !!info.enabled,
    status: info.status ?? 'stopped',
    error: info.error,
  }));
}

/**
 * Toggle a server's PERSISTED `enabled` flag via PATCH, optimistically updating
 * BOTH live copies of the server list — the Settings-panel react-query cache
 * (`['mcp-servers']`) and the chat-toolbar's `settingsStore.mcpServers` — so the
 * change shows immediately in both places with no page refresh. Rolls back both
 * on failure. This is the single source of truth for enabling/disabling an MCP
 * server; there is no separate per-turn override anymore.
 */
export function useMcpToggle(): (name: string, enabled: boolean) => Promise<void> {
  const queryClient = useQueryClient();
  return useCallback(async (name: string, enabled: boolean) => {
    const apply = (val: boolean) => {
      useSettingsStore.getState().setMcpServerEnabled(name, val);
      queryClient.setQueryData<MCPServerInfo[]>(['mcp-servers'], (prev) =>
        (prev ?? []).map((s) => (s.name === name ? { ...s, enabled: val } : s)));
    };
    apply(enabled); // optimistic
    try {
      const resp = await fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      // The skills/autocomplete tool list is filtered server-side by the
      // enabled flags — refetch it so the @-mention list and skills
      // dropdown reflect the toggle immediately, everywhere.
      const { useToolStore } = await import('@/stores/toolStore');
      void useToolStore.getState().fetchMCPTools();
    } catch (err) {
      console.warn('Failed to toggle MCP server:', err);
      apply(!enabled); // rollback
    }
  }, [queryClient]);
}
