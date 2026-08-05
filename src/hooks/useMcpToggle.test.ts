import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// fetchMcpServers calls get() from the api client — mock it.
const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: vi.fn(), del: vi.fn() }));
// The hook dynamically imports the tool store after a successful PATCH.
const { fetchMCPToolsMock } = vi.hoisted(() => ({ fetchMCPToolsMock: vi.fn().mockResolvedValue(undefined) }));
vi.mock('@/stores/toolStore', () => ({
  useToolStore: { getState: () => ({ fetchMCPTools: fetchMCPToolsMock }) },
}));

import { fetchMcpServers, useMcpToggle, type MCPServerInfo } from './useMcpToggle';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

describe('fetchMcpServers', () => {
  beforeEach(() => getMock.mockReset());

  // Regression: the ['mcp-servers'] cache is shared between the MCP settings
  // panel, the API-keys AgentCore toggle, and the toggle hook. A query that
  // returned the raw {servers:{...}} object instead of an array poisoned the
  // cache and made `[...servers]` throw "servers is not iterable". The shared
  // fetcher MUST always return an array.
  it('maps the {servers:{...}} response to an MCPServerInfo[] array', async () => {
    getMock.mockResolvedValue({
      servers: {
        'Bedrock AgentCore': { command: 'uvx', args: ['pkg@latest'], env: {}, enabled: true, status: 'connected' },
        AWS: { command: 'uvx', args: [], enabled: false, status: 'stopped' },
      },
    });

    const list = await fetchMcpServers();

    expect(Array.isArray(list)).toBe(true);
    expect(list.map((s) => s.name).sort()).toEqual(['AWS', 'Bedrock AgentCore']);
    const ac = list.find((s) => s.name === 'Bedrock AgentCore');
    expect(ac?.enabled).toBe(true);
    expect(ac?.args).toEqual(['pkg@latest']);
  });

  it('returns an empty array when servers is missing; a missing enabled flag counts as ON', async () => {
    getMock.mockResolvedValue({});
    expect(await fetchMcpServers()).toEqual([]);

    // Default-enabled contract: the backend treats a missing flag as
    // enabled (and backfills true), so the client must agree.
    getMock.mockResolvedValue({ servers: { Bare: { status: 'stopped' } } });
    const [s] = await fetchMcpServers();
    expect(s).toMatchObject({ name: 'Bare', command: '', args: [], enabled: true });
  });
});

// ---------------------------------------------------------------------------
// useMcpToggle — the single write path shared by the Settings switch and the
// composer's MCP tick. It must keep BOTH live copies of the list in sync:
// the ['mcp-servers'] react-query cache and settingsStore.mcpServers.
// ---------------------------------------------------------------------------

describe('useMcpToggle', () => {
  let queryClient: QueryClient;
  let fetchSpy: ReturnType<typeof vi.fn>;
  let addToastSpy: ReturnType<typeof vi.fn>;
  let loadMCPSpy: ReturnType<typeof vi.fn>;

  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);

  const seed = () => {
    queryClient.setQueryData<MCPServerInfo[]>(['mcp-servers'], [
      { name: 'echo', command: 'python3', args: [], enabled: true, status: 'connected' },
    ]);
    useSettingsStore.setState({
      mcpServers: [{ name: 'echo', status: 'connected', enabled: true }] as never,
      loadMCP: loadMCPSpy as never,
    });
  };

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    addToastSpy = vi.fn().mockReturnValue('toast-id');
    useUIStore.setState({ addToast: addToastSpy as never });
    loadMCPSpy = vi.fn().mockResolvedValue(undefined);
    fetchMCPToolsMock.mockClear();
    seed();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('disables optimistically in BOTH stores, PATCHes, and applies the live stopped status', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null }),
    });

    const { result } = renderHook(() => useMcpToggle(), { wrapper });
    await act(() => result.current('echo', false));

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/mcp/servers/echo',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ enabled: false }) }),
    );
    // Query cache: flag off AND live status applied (dot goes grey with the switch).
    const cached = queryClient.getQueryData<MCPServerInfo[]>(['mcp-servers']);
    expect(cached?.[0]).toMatchObject({ name: 'echo', enabled: false, status: 'stopped' });
    // Store copy: flag off (composer tick), then refetched for live status.
    expect(useSettingsStore.getState().mcpServers[0].enabled).toBe(false);
    expect(loadMCPSpy).toHaveBeenCalled();
    // Tool list refetched so @-mentions drop the server's tools immediately.
    expect(fetchMCPToolsMock).toHaveBeenCalled();
    expect(addToastSpy).not.toHaveBeenCalled();
  });

  it('rolls back BOTH stores and raises an error toast when the PATCH fails', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 500 });

    const { result } = renderHook(() => useMcpToggle(), { wrapper });
    await act(() => result.current('echo', false));

    const cached = queryClient.getQueryData<MCPServerInfo[]>(['mcp-servers']);
    expect(cached?.[0].enabled).toBe(true); // reverted
    expect(useSettingsStore.getState().mcpServers[0].enabled).toBe(true); // reverted
    expect(addToastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringContaining('disable') }),
    );
  });

  it('keeps the flag on but surfaces a connect failure when enabling errors out', async () => {
    // Persisted enabled=true but the live connect failed — the backend
    // returns 200 with status "error"; the switch stays on (that IS the
    // saved state) and the failure is surfaced as a toast.
    queryClient.setQueryData<MCPServerInfo[]>(['mcp-servers'], [
      { name: 'echo', command: 'python3', args: [], enabled: false, status: 'stopped' },
    ]);
    useSettingsStore.setState({
      mcpServers: [{ name: 'echo', status: 'stopped', enabled: false }] as never,
    });
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ name: 'echo', enabled: true, status: 'error', tools: [], error: 'spawn failed' }),
    });

    const { result } = renderHook(() => useMcpToggle(), { wrapper });
    await act(() => result.current('echo', true));

    const cached = queryClient.getQueryData<MCPServerInfo[]>(['mcp-servers']);
    expect(cached?.[0]).toMatchObject({ enabled: true, status: 'error', error: 'spawn failed' });
    expect(addToastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringContaining('spawn failed') }),
    );
  });
});
