/**
 * What the composer sends for the forms of its two menus: `/skill-name` asks
 * for that skill, `/server:prompt` runs the MCP prompt and sends its text as
 * an attachment, and a workspace file or folder chip uploads with the message.
 */
import { render, act, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const h = vi.hoisted(() => ({
  send: vi.fn((_q: string, _o?: unknown): Promise<void> => Promise.resolve()),
  runMcpPrompt: vi.fn(),
  sendWithWorkspaceRefs: vi.fn(),
}));

vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: h.send, sendMidTurn: vi.fn(), abort: vi.fn() }),
}));
vi.mock('@/api/mcp', () => ({ runMcpPrompt: h.runMcpPrompt }));
vi.mock('@/api/agents', () => ({ fetchAgentTypes: () => Promise.resolve([]) }));
vi.mock('@/hooks/chatStream/workspaceFileTurn', () => ({ sendWithWorkspaceRefs: h.sendWithWorkspaceRefs }));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { useSessionStore } from '@/stores/sessionStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useMcpStore, type MCPServerInfo } from '@/stores/mcpStore';
import { useUIStore } from '@/stores/uiStore';
import { useComposerAttachmentsStore } from '@/stores/composerAttachmentsStore';

const SID = 'slash';

const github: MCPServerInfo = {
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
  prompts: [{ name: 'review', command: 'github:review', description: 'Review a PR', arguments: [] }],
  resources: [],
};

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  const { container } = render(<ChatInput sessionId={SID} />, { wrapper });
  return container.querySelector('#chatInput') as HTMLTextAreaElement;
};

/** Type the message, then send it with Cmd+Enter (never a menu pick). */
const sendText = async (ta: HTMLTextAreaElement, value: string) => {
  await act(async () => { fireEvent.change(ta, { target: { value } }); });
  await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter', metaKey: true }); });
};

beforeEach(() => {
  h.send.mockClear();
  h.runMcpPrompt.mockReset();
  h.sendWithWorkspaceRefs.mockClear();
  useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
  useSettingsStore.setState({ skills: [{ name: 'meeting_notes', description: 'Notes', enabled: true }] });
  useMcpStore.setState({ servers: [github], revision: 1 });
  useUIStore.setState({ toasts: [] });
  useComposerAttachmentsStore.setState({ bySession: {} });
});

describe('ChatInput sends', () => {
  it('/skill-name with the user text, asking for that skill', async () => {
    const ta = renderChatInput();
    await sendText(ta, '/meeting_notes tidy these up');

    await waitFor(() => expect(h.send).toHaveBeenCalled());
    expect(h.send).toHaveBeenCalledWith('tidy these up', expect.objectContaining({
      forceSkill: 'meeting_notes',
      displayText: 'tidy these up',
    }));
  });

  it('/server:prompt as the typed text with the prompt text attached', async () => {
    h.runMcpPrompt.mockResolvedValue({ id: 'p1', filename: '/github:review (MCP prompt)' });
    const ta = renderChatInput();
    await sendText(ta, '/github:review 456');

    await waitFor(() => expect(h.send).toHaveBeenCalled());
    expect(h.runMcpPrompt).toHaveBeenCalledWith('github:review', '456');
    expect(h.send).toHaveBeenCalledWith('/github:review 456', expect.objectContaining({
      attachmentIds: ['p1'],
      attachmentNames: ['/github:review (MCP prompt)'],
      forceSkill: undefined,
    }));
  });

  it('nothing when the prompt cannot run: the text stays and a toast says why', async () => {
    h.runMcpPrompt.mockRejectedValue(new Error('/github:review needs: number.'));
    const ta = renderChatInput();
    await sendText(ta, '/github:review');

    await waitFor(() => expect(useUIStore.getState().toasts.map((t) => t.message)).toContain('/github:review needs: number.'));
    expect(h.send).not.toHaveBeenCalled();
    expect(ta.value).toBe('/github:review');
  });

  it('workspace chips through the upload-then-send flow, with the other chips', async () => {
    useComposerAttachmentsStore.setState({
      bySession: {
        [SID]: [
          { id: 'up1', filename: 'notes.txt' },
          { id: 'ws:src', filename: 'src/', workspacePath: 'src', folder: true },
        ],
      },
    });
    const ta = renderChatInput();
    await sendText(ta, 'What is in here?');

    await waitFor(() => expect(h.sendWithWorkspaceRefs).toHaveBeenCalled());
    expect(h.sendWithWorkspaceRefs).toHaveBeenCalledWith(SID, 'What is in here?', expect.objectContaining({
      refs: [{ path: 'src', folder: true }],
      attachmentIds: ['up1'],
      attachmentNames: ['notes.txt'],
    }));
    expect(h.send).not.toHaveBeenCalled();
  });
});
