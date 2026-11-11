/**
 * The composer's menus read the one MCP store, wired the way ChatInput wires
 * them: `/` lists the prompts of the connected servers and `@` their
 * resources. A server added while the app is open (by the assistant, in
 * Settings, by an import) is listed as soon as the store has it.
 */
import { renderHook, act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/client', () => ({ get: vi.fn(), patch: vi.fn() }));

import { useChatAutocomplete } from './useChatAutocomplete';
import { BASE_SLASH_COMMANDS } from '@/components/chat/chatInputConstants';
import { connectedPrompts, connectedResources, useMcpStore, type MCPServerInfo } from '@/stores/mcpStore';

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
    prompts: [],
    resources: [],
    ...over,
  };
}

function useComposerMenus() {
  const servers = useMcpStore((s) => s.servers);
  return useChatAutocomplete({
    text: '',
    setText: () => {},
    textareaRef: { current: null },
    slashCommands: BASE_SLASH_COMMANDS,
    skills: [],
    mcpPrompts: connectedPrompts(servers),
    mcpResources: connectedResources(servers),
    sessions: [],
    agents: [],
    workspace: false,
    onAttachWorkspacePath: () => {},
  });
}

const review = { name: 'review', command: 'github:review', description: 'Review a PR', arguments: [] };
const issue = { uri: 'issue://12', mention: 'github:issue://12', name: 'Issue 12', description: '' };

beforeEach(() => {
  useMcpStore.setState({ servers: [], revision: 1 });
});

describe('composer MCP menus', () => {
  it('list the prompts and resources of a server added after the composer mounted', () => {
    const { result } = renderHook(() => useComposerMenus());

    act(() => { result.current.computeAutocomplete('/git', 4); });
    expect(result.current.acItems).toEqual([]);

    act(() => {
      useMcpStore.setState({ servers: [server('github', { prompts: [review], resources: [issue] })], revision: 2 });
    });
    act(() => { result.current.computeAutocomplete('/git', 4); });
    expect(result.current.acItems.map((i) => i.name)).toEqual(['/github:review']);

    act(() => { result.current.computeAutocomplete('@issue', 6); });
    expect(result.current.acItems.map((i) => i.name)).toEqual(['@github:issue://12']);
  });

  it('leave out a server that is not connected', () => {
    useMcpStore.setState({
      servers: [server('github', { status: 'stopped', enabled: false, prompts: [review], resources: [issue] })],
    });
    const { result } = renderHook(() => useComposerMenus());

    act(() => { result.current.computeAutocomplete('/git', 4); });
    expect(result.current.acItems).toEqual([]);
    act(() => { result.current.computeAutocomplete('@issue', 6); });
    expect(result.current.acItems).toEqual([]);
  });
});
