import { describe, it, expect, vi, beforeEach } from 'vitest';

const { getMock, patchMock } = vi.hoisted(() => ({ getMock: vi.fn(), patchMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, patch: patchMock }));

import { useMcpStore, type MCPServerInfo } from './mcpStore';
import { useUIStore } from './uiStore';

function server(over: Partial<MCPServerInfo> = {}): Omit<MCPServerInfo, 'name'> {
  return {
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

function response(servers: Record<string, Omit<MCPServerInfo, 'name'>>, revision: number) {
  return { servers, revision, config_error: null, pending_elicitations: [] };
}

let addToast: ReturnType<typeof vi.fn>;

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  addToast = vi.fn().mockReturnValue('toast');
  useUIStore.setState({ addToast: addToast as never });
  useMcpStore.setState({ servers: [], pendingElicitations: [], configError: null, revision: -1, loadFailed: false });
});

describe('mcpStore.refresh', () => {
  it('replaces the whole list, so a server removed on the backend does not linger', async () => {
    getMock.mockResolvedValue(response({ echo: server() }, 1));
    await useMcpStore.getState().refresh();
    getMock.mockResolvedValue(response({ other: server() }, 2));
    await useMcpStore.getState().refresh();

    expect(useMcpStore.getState().servers.map((s) => s.name)).toEqual(['other']);
    expect(useMcpStore.getState().revision).toBe(2);
  });

  it('keeps the previous list when a load fails', async () => {
    getMock.mockResolvedValue(response({ echo: server() }, 1));
    await useMcpStore.getState().refresh();
    getMock.mockRejectedValue(new Error('network'));
    await useMcpStore.getState().refresh();

    expect(useMcpStore.getState().servers.map((s) => s.name)).toEqual(['echo']);
    expect(useMcpStore.getState().loadFailed).toBe(true);
  });

  it('shares one request between concurrent calls and runs once more for a call made meanwhile', async () => {
    let release!: (v: unknown) => void;
    getMock.mockReturnValueOnce(new Promise((r) => { release = r; }));
    getMock.mockResolvedValueOnce(response({ late: server() }, 3));

    const first = useMcpStore.getState().refresh();
    const second = useMcpStore.getState().refresh();
    release(response({ early: server() }, 2));
    await Promise.all([first, second]);

    // Two fetches, not one per caller, and the newest state landed last.
    expect(getMock).toHaveBeenCalledTimes(2);
    expect(useMcpStore.getState().servers.map((s) => s.name)).toEqual(['late']);
  });
});

describe('mcpStore.applyChange', () => {
  it('refetches for a revision it does not hold and skips the one it has', async () => {
    getMock.mockResolvedValue(response({ echo: server() }, 5));
    await useMcpStore.getState().refresh();
    getMock.mockClear();

    useMcpStore.getState().applyChange(5);
    expect(getMock).not.toHaveBeenCalled();

    getMock.mockResolvedValue(response({ echo: server(), added: server() }, 6));
    useMcpStore.getState().applyChange(6);
    await vi.waitFor(() => expect(useMcpStore.getState().revision).toBe(6));
    expect(useMcpStore.getState().servers.map((s) => s.name)).toEqual(['echo', 'added']);
  });

  it('refetches after a backend restart restarts the counter', async () => {
    getMock.mockResolvedValue(response({ echo: server() }, 9));
    await useMcpStore.getState().refresh();
    getMock.mockResolvedValue(response({ echo: server() }, 1));
    useMcpStore.getState().applyChange(1);
    await vi.waitFor(() => expect(useMcpStore.getState().revision).toBe(1));
  });
});

describe('mcpStore.setEnabled', () => {
  beforeEach(() => {
    useMcpStore.setState({ servers: [{ name: 'echo', ...server() }], revision: 1 });
  });

  it('moves the switch at once, PATCHes, then reloads the live status', async () => {
    let release!: (v: unknown) => void;
    patchMock.mockReturnValue(new Promise((r) => { release = r; }));
    getMock.mockResolvedValue(response({ echo: server({ enabled: false, status: 'stopped' }) }, 2));

    const done = useMcpStore.getState().setEnabled('echo', false);
    expect(useMcpStore.getState().servers[0].enabled).toBe(false); // before the response
    expect(patchMock).toHaveBeenCalledWith('/api/mcp/servers/echo', { enabled: false });

    release({ name: 'echo', enabled: false, status: 'stopped', tools: [], error: null });
    await done;
    expect(useMcpStore.getState().servers[0]).toMatchObject({ enabled: false, status: 'stopped' });
    expect(addToast).not.toHaveBeenCalled();
  });

  it('rolls back and raises an error toast when the PATCH fails', async () => {
    patchMock.mockRejectedValue(new Error('HTTP 500'));
    await useMcpStore.getState().setEnabled('echo', false);

    expect(useMcpStore.getState().servers[0].enabled).toBe(true);
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringContaining('disable') }),
    );
  });

  it('keeps the switch on but surfaces a failed connect', async () => {
    useMcpStore.setState({ servers: [{ name: 'echo', ...server({ enabled: false, status: 'stopped' }) }] });
    patchMock.mockResolvedValue({ name: 'echo', enabled: true, status: 'error', tools: [], error: 'spawn failed' });
    getMock.mockResolvedValue(response({ echo: server({ status: 'error', error: 'spawn failed' }) }, 2));

    await useMcpStore.getState().setEnabled('echo', true);

    expect(useMcpStore.getState().servers[0]).toMatchObject({ enabled: true, status: 'error' });
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringContaining('spawn failed') }),
    );
  });
});
