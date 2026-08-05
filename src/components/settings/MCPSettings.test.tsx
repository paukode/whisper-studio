import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// settingsStore / the panel import the api client; the list query goes
// through get(). Mutations (post/put/del) are not exercised here.
const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: vi.fn(), del: vi.fn() }));
const { fetchMCPToolsMock } = vi.hoisted(() => ({ fetchMCPToolsMock: vi.fn().mockResolvedValue(undefined) }));
vi.mock('@/stores/toolStore', () => ({
  useToolStore: { getState: () => ({ fetchMCPTools: fetchMCPToolsMock }) },
}));

import { MCPSettings } from './MCPSettings';
import { MoreMenu } from '@/components/chat/MoreMenu';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

const SERVERS_RESPONSE = {
  servers: {
    echo: { command: 'python3', args: ['echo.py'], env: {}, enabled: true, status: 'connected', error: null },
    other: { command: 'npx', args: [], env: {}, enabled: false, status: 'stopped', error: null },
  },
};

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

describe('MCPSettings — enabled switch', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(SERVERS_RESPONSE);
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({
      mcpServers: [
        { name: 'echo', status: 'connected', enabled: true },
        { name: 'other', status: 'stopped', enabled: false },
      ] as never,
      loadMCP: vi.fn().mockResolvedValue(undefined) as never,
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it('renders the Skills-style toggle-switch (not a bare checkbox) reflecting each enabled flag', async () => {
    const { container } = renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('echo')).toBeInTheDocument());

    const switches = container.querySelectorAll('label.toggle-switch');
    expect(switches).toHaveLength(2);
    // Same markup contract as SkillsPanel: input + .toggle-slider inside the label.
    expect(container.querySelectorAll('label.toggle-switch .toggle-slider')).toHaveLength(2);

    const echoSwitch = screen.getByRole('switch', { name: /disable mcp server echo/i });
    const otherSwitch = screen.getByRole('switch', { name: /enable mcp server other/i });
    expect(echoSwitch).toBeChecked();
    expect(otherSwitch).not.toBeChecked();
  });

  it('toggles optimistically and PATCHes the shared endpoint', async () => {
    // Deferred fetch: hold the PATCH open so the optimistic (pre-response)
    // state is observable.
    let resolveFetch!: (v: unknown) => void;
    fetchSpy.mockReturnValue(new Promise((r) => { resolveFetch = r; }));
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('echo')).toBeInTheDocument());

    const echoSwitch = screen.getByRole('switch', { name: /disable mcp server echo/i });
    fireEvent.click(echoSwitch);

    // Optimistic: unchecked while the PATCH is still in flight.
    await waitFor(() => expect(echoSwitch).not.toBeChecked());
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/mcp/servers/echo',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ enabled: false }) }),
    );
    // The composer's store copy flipped too (same write path).
    expect(useSettingsStore.getState().mcpServers.find((s) => s.name === 'echo')?.enabled).toBe(false);

    resolveFetch({
      ok: true,
      json: async () => ({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null }),
    });
    await waitFor(() => expect(echoSwitch).not.toBeChecked()); // stays off after the live echo
  });

  it('reverts the switch and surfaces an error toast when the PATCH fails', async () => {
    let resolveFetch!: (v: unknown) => void;
    fetchSpy.mockReturnValue(new Promise((r) => { resolveFetch = r; }));
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('echo')).toBeInTheDocument());

    const echoSwitch = screen.getByRole('switch', { name: /disable mcp server echo/i });
    fireEvent.click(echoSwitch);
    await waitFor(() => expect(echoSwitch).not.toBeChecked()); // optimistic

    resolveFetch({ ok: false, status: 500 });
    await waitFor(() => expect(echoSwitch).toBeChecked()); // reverted
    expect(useSettingsStore.getState().mcpServers.find((s) => s.name === 'echo')?.enabled).toBe(true);
    expect(useUIStore.getState().addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error' }),
    );
  });
});

describe('Settings switch and composer tick stay in sync', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  const moreMenuProps = {
    open: true,
    section: 'mcp' as const,
    setSection: vi.fn(),
    onToggle: vi.fn(),
    onClose: vi.fn(),
    indexes: [],
    selectedIndexes: [],
    toggleIndex: vi.fn(),
    wsConnected: false,
    onInsertSkill: vi.fn(),
  };

  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(SERVERS_RESPONSE);
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({
      mcpServers: [{ name: 'echo', status: 'connected', enabled: true }] as never,
      skills: [] as never,
      loadMCP: vi.fn().mockResolvedValue(undefined) as never,
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it('unticking in the composer turns the Settings switch off (one shared state, one endpoint)', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null }),
    });
    const { container } = renderWithClient(
      <>
        <MCPSettings />
        <MoreMenu {...moreMenuProps} />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: /disable mcp server echo/i })).toBeChecked(),
    );

    // The composer row's tick (checkbox inside the MoreMenu's MCP section).
    const composerTick = container.querySelector<HTMLInputElement>('#more-sec-mcp input[type="checkbox"]');
    expect(composerTick).not.toBeNull();
    fireEvent.click(composerTick!);

    await waitFor(() =>
      expect(screen.getByRole('switch', { name: /enable mcp server echo/i })).not.toBeChecked(),
    );
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/mcp/servers/echo',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ enabled: false }) }),
    );
  });

  it('flipping the Settings switch unticks the composer row', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null }),
    });
    const { container } = renderWithClient(
      <>
        <MCPSettings />
        <MoreMenu {...moreMenuProps} />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: /disable mcp server echo/i })).toBeChecked(),
    );
    const composerTick = container.querySelector<HTMLInputElement>('#more-sec-mcp input[type="checkbox"]');
    expect(composerTick).toBeChecked();

    fireEvent.click(screen.getByRole('switch', { name: /disable mcp server echo/i }));

    await waitFor(() => expect(composerTick).not.toBeChecked());
  });
});
