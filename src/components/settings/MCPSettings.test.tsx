import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// settingsStore / the panel import the api client; the list query goes
// through get(). Mutations (post/put/del) are exercised in the
// remote/approval-granularity and elicitation describe blocks below.
const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: postMock, del: vi.fn() }));

import { MCPSettings, parseMcpArgs } from './MCPSettings';
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

// Enabling/disabling MCP servers now lives ONLY in Settings → MCP. The composer
// MoreMenu used to carry a per-server tick that shared this write path; that UI
// was removed, so the menu must no longer render any MCP section or toggle even
// when a server is configured. (The model still gets enabled servers' tools via
// the backend; @-mention insertion is unaffected — it lives in ChatInput.)
describe('composer MoreMenu has no MCP toggle (moved to Settings → MCP)', () => {
  const moreMenuProps = {
    open: true,
    section: null,
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
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({
      mcpServers: [{ name: 'echo', status: 'connected', enabled: true }] as never,
      skills: [] as never,
      loadMCP: vi.fn().mockResolvedValue(undefined) as never,
    });
  });

  it('renders no MCP section, tick, or "MCP servers" row even with a server configured', () => {
    const { container } = renderWithClient(<MoreMenu {...moreMenuProps} />);
    // The old MCP section and its per-server checkbox tick are gone.
    expect(container.querySelector('#more-sec-mcp')).toBeNull();
    expect(container.querySelector('#more-sec-mcp input[type="checkbox"]')).toBeNull();
    expect(screen.queryByText('MCP servers')).toBeNull();
    // The menu itself is intact (the Skills control still renders).
    expect(screen.getByText('Skills')).toBeInTheDocument();
  });
});

describe('parseMcpArgs — args field parsing', () => {
  it('parses a JSON array pasted with curly/smart quotes (the AgentCore paste)', () => {
    const raw = '[“awslabs.amazon-bedrock-agentcore-mcp-server@latest”]';
    expect(parseMcpArgs(raw)).toEqual({
      args: ['awslabs.amazon-bedrock-agentcore-mcp-server@latest'],
    });
  });

  it('parses a normal straight-quote JSON array', () => {
    expect(parseMcpArgs('["--flag", "value"]')).toEqual({ args: ['--flag', 'value'] });
  });

  it('returns [] for empty input', () => {
    expect(parseMcpArgs('   ')).toEqual({ args: [] });
  });

  it('errors on non-JSON input (no comma-split fallback)', () => {
    expect('error' in parseMcpArgs('--flag, value')).toBe(true);
  });

  it('errors on a bracketed-but-invalid value instead of wrapping it as one arg', () => {
    expect('error' in parseMcpArgs('[not, valid, json]')).toBe(true);
  });
});

describe('MCPSettings — remote/approval-granularity fields', () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    getMock.mockResolvedValue({ servers: {} });
    postMock.mockResolvedValue({});
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({
      mcpServers: [] as never,
      loadMCP: vi.fn().mockResolvedValue(undefined) as never,
      loadSkills: vi.fn().mockResolvedValue(undefined) as never,
    });
  });

  it('saving a new server with a URL (no command) posts url/bearer_token_env_var/approval_mode', async () => {
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('+ Add Server')).toBeInTheDocument());

    fireEvent.click(screen.getByText('+ Add Server'));
    fireEvent.change(screen.getByPlaceholderText('Server name'), { target: { value: 'remote-srv' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. https://example.com/mcp'), {
      target: { value: 'https://example.com/mcp' },
    });
    fireEvent.change(
      screen.getByPlaceholderText('e.g. MY_SERVER_TOKEN — the env var NAME, never the token itself'),
      { target: { value: 'MY_SERVER_TOKEN' } },
    );
    fireEvent.change(screen.getByDisplayValue('auto — never ask'), { target: { value: 'prompt' } });

    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith(
      '/api/mcp/servers',
      expect.objectContaining({
        name: 'remote-srv',
        command: '',
        url: 'https://example.com/mcp',
        bearer_token_env_var: 'MY_SERVER_TOKEN',
        approval_mode: 'prompt',
      }),
    );
  });

  it('does not save when neither command nor url is provided', async () => {
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('+ Add Server')).toBeInTheDocument());

    fireEvent.click(screen.getByText('+ Add Server'));
    fireEvent.change(screen.getByPlaceholderText('Server name'), { target: { value: 'no-transport' } });
    fireEvent.click(screen.getByText('Save'));

    // Neither command nor url filled in -> handleSave bails before posting.
    await new Promise((r) => setTimeout(r, 0));
    expect(postMock).not.toHaveBeenCalled();
  });

  it('saving with tool_overrides/enabled_tools/disabled_tools parses them correctly', async () => {
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('+ Add Server')).toBeInTheDocument());

    fireEvent.click(screen.getByText('+ Add Server'));
    fireEvent.change(screen.getByPlaceholderText('Server name'), { target: { value: 'srv' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. npx'), { target: { value: 'npx' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. {"delete_all": "approve"}'), {
      target: { value: '{"delete_all": "approve"}' },
    });
    fireEvent.change(screen.getByPlaceholderText('e.g. get_data, search_items'), {
      target: { value: 'get_data, search_items' },
    });
    fireEvent.change(screen.getByPlaceholderText('e.g. delete_all'), { target: { value: 'delete_all' } });

    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith(
      '/api/mcp/servers',
      expect.objectContaining({
        tool_overrides: { delete_all: 'approve' },
        enabled_tools: ['get_data', 'search_items'],
        disabled_tools: ['delete_all'],
      }),
    );
  });

  it('rejects an invalid tool_overrides mode instead of silently posting it', async () => {
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText('+ Add Server')).toBeInTheDocument());

    fireEvent.click(screen.getByText('+ Add Server'));
    fireEvent.change(screen.getByPlaceholderText('Server name'), { target: { value: 'srv' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. npx'), { target: { value: 'npx' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. {"delete_all": "approve"}'), {
      target: { value: '{"delete_all": "not-a-real-mode"}' },
    });

    fireEvent.click(screen.getByText('Save'));

    await waitFor(() =>
      expect(screen.getByText(/tool overrides must be a json object/i)).toBeInTheDocument(),
    );
    expect(postMock).not.toHaveBeenCalled();
  });
});

describe('MCPSettings — pending elicitations', () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    postMock.mockResolvedValue({ ok: true });
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({
      mcpServers: [] as never,
      loadMCP: vi.fn().mockResolvedValue(undefined) as never,
      loadSkills: vi.fn().mockResolvedValue(undefined) as never,
    });
  });

  const RESPONSE_WITH_ELICITATION = {
    servers: {},
    pending_elicitations: [
      {
        elicitation_id: 'elicit-1',
        server: 'weather',
        session_id: 'chat-1',
        mode: 'form',
        message: 'Please provide your API key',
        requested_schema: { type: 'object', properties: { key: { type: 'string' } } },
        url: null,
      },
    ],
  };

  it('renders a pending elicitation with the server name and message', async () => {
    getMock.mockResolvedValue(RESPONSE_WITH_ELICITATION);
    renderWithClient(<MCPSettings />);

    await waitFor(() => expect(screen.getByText(/weather needs input/i)).toBeInTheDocument());
    expect(screen.getByText('Please provide your API key')).toBeInTheDocument();
  });

  it('Accept posts mcp_elicit_respond with the parsed JSON content', async () => {
    getMock.mockResolvedValue(RESPONSE_WITH_ELICITATION);
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText(/weather needs input/i)).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"key": "abc123"}' } });
    fireEvent.click(screen.getByText('Accept'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith('/api/approval/execute', {
      action: 'mcp_elicit_respond',
      payload: { elicitation_id: 'elicit-1', response_action: 'accept', content: { key: 'abc123' } },
    });
  });

  it('Decline posts mcp_elicit_respond with no content', async () => {
    getMock.mockResolvedValue(RESPONSE_WITH_ELICITATION);
    renderWithClient(<MCPSettings />);
    await waitFor(() => expect(screen.getByText(/weather needs input/i)).toBeInTheDocument());

    fireEvent.click(screen.getByText('Decline'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith('/api/approval/execute', {
      action: 'mcp_elicit_respond',
      payload: { elicitation_id: 'elicit-1', response_action: 'decline', content: undefined },
    });
  });

  it('renders nothing extra when there are no pending elicitations', async () => {
    getMock.mockResolvedValue({ servers: {}, pending_elicitations: [] });
    renderWithClient(<MCPSettings />);

    await waitFor(() => expect(screen.getByText('No MCP servers configured.')).toBeInTheDocument());
    expect(screen.queryByText(/needs input/i)).toBeNull();
  });
});
