/**
 * The composer's `/mcp:` and `@mcp:` menus read the one MCP store, wired the
 * way ChatInput wires them. A server added while the app is open (by the
 * assistant, in Settings, by an import) is listed as soon as the store has
 * it, with its tool count, instead of the list loaded at startup.
 */
import { renderHook, act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/client', () => ({ get: vi.fn(), patch: vi.fn() }));

import { useChatAutocomplete } from './useChatAutocomplete';
import { BASE_SLASH_COMMANDS } from '@/components/chat/chatInputConstants';
import { useMcpStore, type MCPServerInfo } from '@/stores/mcpStore';

function server(name: string, over: Partial<MCPServerInfo> = {}): MCPServerInfo {
  return {
    name,
    command: name,
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

function useComposerMenus() {
  const mcpServers = useMcpStore((s) => s.servers);
  return useChatAutocomplete({
    text: '',
    setText: () => {},
    textareaRef: { current: null },
    slashCommands: BASE_SLASH_COMMANDS,
    skills: [],
    mcpServers,
    sessions: [],
  });
}

beforeEach(() => {
  useMcpStore.setState({ servers: [], revision: 1 });
});

describe('composer MCP menus', () => {
  it('list a server added after the composer mounted', () => {
    const { result } = renderHook(() => useComposerMenus());

    act(() => { result.current.computeAutocomplete('/mcp:', 5); });
    expect(result.current.acItems.map((i) => i.name)).toEqual(['No matches']);

    act(() => {
      useMcpStore.setState({
        servers: [server('aws-sentral-mcp', { tools: Array.from({ length: 70 }, (_, i) => `t${i}`) })],
        revision: 2,
      });
    });
    act(() => { result.current.computeAutocomplete('/mcp:', 5); });
    expect(result.current.acItems).toEqual([
      expect.objectContaining({ name: 'aws-sentral-mcp', desc: 'MCP server (connected, 70 tools)' }),
    ]);

    act(() => { result.current.computeAutocomplete('@mcp:', 5); });
    expect(result.current.acItems.map((i) => i.name)).toEqual(['aws-sentral-mcp']);
  });

  it('show a connected server with nothing callable, and an off one', () => {
    useMcpStore.setState({
      servers: [server('creds-agent'), server('spare', { enabled: false, status: 'stopped' })],
    });
    const { result } = renderHook(() => useComposerMenus());

    act(() => { result.current.computeAutocomplete('/mcp:', 5); });
    expect(result.current.acItems.map((i) => i.desc)).toEqual([
      'MCP server (connected, 0 tools)',
      'MCP server (off)',
    ]);
  });
});
