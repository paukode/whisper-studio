import { describe, it, expect, vi, beforeEach } from 'vitest';

// fetchMcpServers calls get() from the api client — mock it.
const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock }));

import { fetchMcpServers } from './useMcpToggle';

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

  it('returns an empty array when servers is missing or fields are absent', async () => {
    getMock.mockResolvedValue({});
    expect(await fetchMcpServers()).toEqual([]);

    getMock.mockResolvedValue({ servers: { Bare: { status: 'stopped' } } });
    const [s] = await fetchMcpServers();
    expect(s).toMatchObject({ name: 'Bare', command: '', args: [], enabled: false });
  });
});
