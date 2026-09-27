/**
 * The autocomplete popup's heading names the menu that is open.
 *
 * It used to read "Skills" over every list, the command list and the @
 * mentions included. The composer now sets the open menu's name as the
 * popup's data-heading, and the stylesheet shows that attribute.
 */
import { render, act, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() }),
}));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { useSessionStore } from '@/stores/sessionStore';
import { useMcpStore } from '@/stores/mcpStore';
import { useSettingsStore } from '@/stores/settingsStore';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  const { container } = render(<ChatInput sessionId="ac" />, { wrapper });
  return {
    textarea: container.querySelector('#chatInput') as HTMLTextAreaElement,
    heading: () => container.querySelector('[role="listbox"]')?.getAttribute('data-heading'),
  };
};

const type = async (el: HTMLTextAreaElement, value: string) => {
  await act(async () => { fireEvent.change(el, { target: { value } }); });
};

describe('ChatInput autocomplete heading', () => {
  beforeEach(() => {
    useSessionStore.setState({ currentSessionId: 'ac', liveSessions: {}, sessions: [] });
    useSettingsStore.setState({
      skills: [{ name: 'translate-text', description: 'Translate text', enabled: true }],
    });
    // MCP servers live in their own store; a revision marks the list as loaded.
    useMcpStore.setState({
      servers: [
        {
          name: 'github',
          command: 'github',
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
        },
      ],
      revision: 1,
    });
  });

  it.each([
    ['/', 'Commands'],
    ['/sk', 'Commands'],
    ['/effort ', 'Options'],
    ['/file:', 'Files'],
    ['/skills:', 'Skills'],
    ['/mcp:', 'MCP servers'],
    ['@', 'Mentions'],
    ['@file:', 'Files'],
    ['@skills:', 'Skills'],
    ['@mcp:', 'MCP servers'],
  ])('typing %s opens a menu headed %s', async (input, expected) => {
    const { textarea, heading } = renderChatInput();
    await type(textarea, input);
    // The file menus list their placeholder row after the 200ms search debounce.
    await waitFor(() => expect(heading()).toBe(expected));
  });

  it.each([
    ['/sk', 'Commands', 'Skills'],
    ['/eff', 'Commands', 'Options'],
    ['@mc', 'Mentions', 'MCP servers'],
  ])('picking the match for %s turns the %s heading into %s', async (input, before, after) => {
    const { textarea, heading } = renderChatInput();
    await type(textarea, input);
    expect(heading()).toBe(before);
    await act(async () => { fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' }); });
    expect(heading()).toBe(after);
  });
});
