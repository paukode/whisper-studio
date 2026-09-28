import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The panel reads the one MCP store (mcpStore); its refresh goes through
// get() and the enable switch through patch(). Mutations (post/put/del) are
// exercised in the remote/approval-granularity and elicitation blocks below.
const { getMock, postMock, patchMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  patchMock: vi.fn(),
}));
vi.mock('@/api/client', () => ({ get: getMock, put: vi.fn(), post: postMock, del: vi.fn(), patch: patchMock }));

import { MCPSettings, parseMcpArgs } from './MCPSettings';
import { MoreMenu } from '@/components/chat/MoreMenu';
import { useMcpStore, type MCPServerInfo } from '@/stores/mcpStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

function server(name: string, over: Partial<MCPServerInfo> = {}): MCPServerInfo {
  return {
    name,
    command: 'python3',
    args: [],
    env: {},
    enabled: true,
    status: 'connected',
    tools: [],
    error: null,
    url: '',
    bearer_token_env_var: '',
    approval_mode: 'auto',
    tool_overrides: {},
    enabled_tools: [],
    disabled_tools: [],
    ...over,
  };
}

/** The GET /api/mcp/servers body for a list of servers. */
function responseFor(servers: MCPServerInfo[], revision = 1) {
  return {
    servers: Object.fromEntries(servers.map(({ name, ...info }) => [name, info])),
    revision,
    config_error: null,
    pending_elicitations: [],
  };
}

function seed(servers: MCPServerInfo[], extra: Partial<ReturnType<typeof useMcpStore.getState>> = {}) {
  useMcpStore.setState({
    servers,
    pendingElicitations: [],
    configError: null,
    revision: 1,
    loadFailed: false,
    ...extra,
  });
}

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

const ECHO = server('echo', { args: ['echo.py'], tools: ['mcp__echo__ping', 'mcp__echo__pong'] });
const OTHER = server('other', { command: 'npx', enabled: false, status: 'stopped' });

describe('MCPSettings: enabled switch', () => {
  beforeEach(() => {
    getMock.mockReset();
    patchMock.mockReset();
    getMock.mockResolvedValue(responseFor([ECHO, OTHER]));
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    seed([ECHO, OTHER]);
  });

  it('renders the Skills-style toggle-switch (not a bare checkbox) reflecting each enabled flag', () => {
    const { container } = renderWithClient(<MCPSettings />);
    expect(screen.getByText('echo')).toBeInTheDocument();

    const switches = container.querySelectorAll('label.toggle-switch');
    expect(switches).toHaveLength(2);
    // Same markup contract as SkillsPanel: input + .toggle-slider inside the label.
    expect(container.querySelectorAll('label.toggle-switch .toggle-slider')).toHaveLength(2);

    expect(screen.getByRole('switch', { name: /disable mcp server echo/i })).toBeChecked();
    expect(screen.getByRole('switch', { name: /enable mcp server other/i })).not.toBeChecked();
  });

  it('toggles optimistically and PATCHes the shared endpoint', async () => {
    // Deferred PATCH: hold it open so the optimistic state is observable.
    let resolvePatch!: (v: unknown) => void;
    patchMock.mockReturnValue(new Promise((r) => { resolvePatch = r; }));
    renderWithClient(<MCPSettings />);

    const echoSwitch = screen.getByRole('switch', { name: /disable mcp server echo/i });
    fireEvent.click(echoSwitch);

    await waitFor(() => expect(echoSwitch).not.toBeChecked());
    expect(patchMock).toHaveBeenCalledWith('/api/mcp/servers/echo', { enabled: false });
    // The composer reads the same store, so its copy flipped too.
    expect(useMcpStore.getState().servers.find((s) => s.name === 'echo')?.enabled).toBe(false);

    getMock.mockResolvedValue(responseFor([{ ...ECHO, enabled: false, status: 'stopped' }, OTHER], 2));
    resolvePatch({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null });
    await waitFor(() => expect(useMcpStore.getState().revision).toBe(2));
    expect(echoSwitch).not.toBeChecked();
  });

  it('reverts the switch and surfaces an error toast when the PATCH fails', async () => {
    let rejectPatch!: (e: unknown) => void;
    patchMock.mockReturnValue(new Promise((_r, rej) => { rejectPatch = rej; }));
    renderWithClient(<MCPSettings />);

    const echoSwitch = screen.getByRole('switch', { name: /disable mcp server echo/i });
    fireEvent.click(echoSwitch);
    await waitFor(() => expect(echoSwitch).not.toBeChecked()); // optimistic

    rejectPatch(new Error('HTTP 500'));
    await waitFor(() => expect(echoSwitch).toBeChecked()); // reverted
    expect(useUIStore.getState().addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error' }),
    );
  });
});

describe('MCPSettings: one live list', () => {
  beforeEach(() => {
    getMock.mockReset();
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    seed([ECHO]);
  });

  it('shows a server added elsewhere (the assistant, an editor) without reopening', async () => {
    renderWithClient(<MCPSettings />);
    expect(screen.queryByText('creds-agent')).toBeNull();

    getMock.mockResolvedValue(
      responseFor([ECHO, server('creds-agent', { command: 'aim', args: ['mcp', 'start-server'] })], 2),
    );
    // What the shared event channel does on a backend mcp_changed event.
    act(() => useMcpStore.getState().applyChange(2));

    await waitFor(() => expect(screen.getByText('creds-agent')).toBeInTheDocument());
  });

  it('shows each server\'s tool count, or why it failed', () => {
    seed([ECHO, server('broken', { status: 'error', error: 'Command "aim" was not found.' })]);
    renderWithClient(<MCPSettings />);

    expect(screen.getByText('python3 echo.py · 2 tools')).toBeInTheDocument();
    expect(screen.getByText('python3 · Command "aim" was not found.')).toBeInTheDocument();
  });

  it('reports an unreadable mcp_servers.json', () => {
    seed([ECHO], { configError: 'mcp_servers.json is not a valid server list' });
    renderWithClient(<MCPSettings />);
    expect(screen.getByText('mcp_servers.json is not a valid server list')).toBeInTheDocument();
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
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    useSettingsStore.setState({ skills: [] as never });
    seed([ECHO]);
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
    getMock.mockResolvedValue(responseFor([]));
    postMock.mockResolvedValue({});
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    seed([]);
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

describe('MCPSettings: pending elicitations', () => {
  const ELICITATION = {
    elicitation_id: 'elicit-1',
    server: 'weather',
    session_id: 'chat-1',
    mode: 'form',
    message: 'Please provide your API key',
    requested_schema: { type: 'object', properties: { key: { type: 'string' } } },
    url: null,
  };

  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    postMock.mockResolvedValue({ ok: true });
    getMock.mockResolvedValue(responseFor([]));
    useUIStore.setState({ addToast: vi.fn().mockReturnValue('t') as never });
    seed([], { pendingElicitations: [ELICITATION] });
  });

  it('renders a pending elicitation with the server name and message', () => {
    renderWithClient(<MCPSettings />);
    expect(screen.getByText(/weather needs input/i)).toBeInTheDocument();
    expect(screen.getByText('Please provide your API key')).toBeInTheDocument();
  });

  it('Accept posts mcp_elicit_respond with the parsed JSON content', async () => {
    renderWithClient(<MCPSettings />);

    fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"key": "abc123"}' } });
    fireEvent.click(screen.getByText('Accept'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith('/api/approval/execute', {
      action: 'mcp_elicit_respond',
      payload: { elicitation_id: 'elicit-1', response_action: 'accept', content: { key: 'abc123' } },
    });
  });

  it('Decline posts mcp_elicit_respond with no content', async () => {
    renderWithClient(<MCPSettings />);

    fireEvent.click(screen.getByText('Decline'));

    await waitFor(() => expect(postMock).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith('/api/approval/execute', {
      action: 'mcp_elicit_respond',
      payload: { elicitation_id: 'elicit-1', response_action: 'decline', content: undefined },
    });
  });

  it('renders nothing extra when there are no pending elicitations', () => {
    seed([]);
    renderWithClient(<MCPSettings />);

    expect(screen.getByText('No MCP servers configured.')).toBeInTheDocument();
    expect(screen.queryByText(/needs input/i)).toBeNull();
  });
});
