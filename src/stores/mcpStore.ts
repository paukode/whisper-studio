import { create } from 'zustand';
import { get, patch } from '@/api/client';
import { MCPServersResponseSchema } from '@/types/schemas';
import { useUIStore } from './uiStore';

/** One configured MCP server with its live status, as GET /api/mcp/servers
 *  reports it (server/mcp_routes.py). */
export interface MCPServerInfo {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
  status: string;
  /** Registry keys (mcp__server__tool) of the tools the server advertises. */
  tools: string[];
  error: string | null;
  url: string;
  bearer_token_env_var: string;
  approval_mode: string;
  tool_overrides: Record<string, string>;
  enabled_tools: string[];
  disabled_tools: string[];
}

/** An MCP server asking the human for input mid tool-call. */
export interface PendingElicitation {
  elicitation_id: string;
  server: string;
  session_id?: string | null;
  mode: string;
  message: string;
  requested_schema?: Record<string, unknown> | null;
  url?: string | null;
}

interface McpServersResponse {
  servers: Record<string, Omit<MCPServerInfo, 'name'>>;
  revision: number;
  config_error: string | null;
  pending_elicitations: PendingElicitation[];
}

interface McpState {
  servers: MCPServerInfo[];
  pendingElicitations: PendingElicitation[];
  /** Why the backend could not read mcp_servers.json, or null. */
  configError: string | null;
  /** The backend revision this list reflects; -1 before the first load. */
  revision: number;
  /** The last load failed; the previous list is kept. */
  loadFailed: boolean;
  /** Fetch the list. Concurrent calls share one request, and a call made
   *  while one is in flight runs once more after it, so the newest state
   *  always lands last. */
  refresh: () => Promise<void>;
  /** A `mcp_changed` event from the backend: refetch unless this revision is
   *  already held. Compared for equality, not order, because a backend
   *  restart starts counting again. */
  applyChange: (revision: number) => void;
  /** Persist a server's enabled flag. The switch moves at once and rolls
   *  back with an error toast if the request fails. */
  setEnabled: (name: string, enabled: boolean) => Promise<void>;
}

let inFlight: Promise<void> | null = null;
let again = false;

async function fetchOnce(): Promise<void> {
  try {
    const data = await get<McpServersResponse>('/api/mcp/servers', { schema: MCPServersResponseSchema });
    useMcpStore.setState({
      servers: Object.entries(data.servers).map(([name, info]) => ({ name, ...info })),
      pendingElicitations: data.pending_elicitations,
      configError: data.config_error,
      revision: data.revision,
      loadFailed: false,
    });
  } catch (err) {
    // Keep the previous list: a network blip must not empty every menu.
    console.warn('Failed to load MCP servers:', err);
    useMcpStore.setState({ loadFailed: true });
  }
}

/** The one MCP server list: Settings > MCP, the composer's `/mcp:` and
 *  `@mcp:` menus, and the import panel all read it. The backend pushes
 *  `mcp_changed` on the shared session-events stream whenever the list
 *  changes (sessionRuntimes.ts), so a server added from anywhere, including
 *  by the assistant mid-session, shows up everywhere without a restart. */
export const useMcpStore = create<McpState>((set, getState) => ({
  servers: [],
  pendingElicitations: [],
  configError: null,
  revision: -1,
  loadFailed: false,

  refresh: () => {
    if (inFlight) {
      again = true;
      return inFlight;
    }
    inFlight = (async () => {
      try {
        do {
          again = false;
          await fetchOnce();
        } while (again);
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  },

  applyChange: (revision) => {
    if (revision === getState().revision && !inFlight) return;
    void getState().refresh();
  },

  setEnabled: async (name, enabled) => {
    const flip = (value: boolean) =>
      set((s) => ({ servers: s.servers.map((srv) => (srv.name === name ? { ...srv, enabled: value } : srv)) }));
    flip(enabled);
    let live: { status?: string; error?: string | null };
    try {
      live = await patch(`/api/mcp/servers/${encodeURIComponent(name)}`, { enabled });
    } catch (err) {
      console.warn('Failed to toggle MCP server:', err);
      flip(!enabled);
      useUIStore.getState().addToast({
        type: 'error',
        message: `Failed to ${enabled ? 'enable' : 'disable'} MCP server "${name}"`,
        source: 'mcp',
      });
      return;
    }
    await getState().refresh();
    if (enabled && live.status === 'error') {
      // The flag persisted but the server failed to connect: the switch
      // stays on (that is the saved state) and the failure is surfaced.
      useUIStore.getState().addToast({
        type: 'error',
        message: `MCP server "${name}" failed to connect${live.error ? `: ${live.error}` : ''}`,
        source: 'mcp',
      });
    }
  },
}));
