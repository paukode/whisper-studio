/**
 * Disconnect workspace inside the real AppShell: instant, and it never takes
 * the session's work down with it.
 *
 * Reported: "sometimes the Disconnect button does nothing and freezes,
 * particularly when a session is in progress". Three client causes, each
 * pinned here against the real WorkspacePanel and TerminalPanel:
 *  - the panel only changed after the POST returned, so a request queued
 *    behind the turn's streams looked like a dead button;
 *  - a focus resync GET sent on the click's mousedown answered "connected"
 *    after the disconnect and put the workspace back on screen;
 *  - hiding the panel unmounted TerminalPanel, which killed every open PTY
 *    (the dev server the session was running in it).
 */
import { render, fireEvent } from '@testing-library/react';
import { act } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('./Sidebar', () => ({ Sidebar: () => <div /> }));
vi.mock('./Header', () => ({ Header: () => <div /> }));
vi.mock('./AppStatusBar', () => ({ AppStatusBar: () => <div /> }));
vi.mock('@/components/chat/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }));
vi.mock('@/components/transcription/TranscriptionPanel', () => ({ TranscriptionPanel: () => <div /> }));
vi.mock('@/components/preview/RightDock', () => ({ RightDock: () => <div /> }));
vi.mock('@/components/common/ToastContainer', () => ({ ToastContainer: () => <div /> }));
vi.mock('@/components/common/ModelLoadingBanner', () => ({ ModelLoadingBanner: () => <div /> }));
vi.mock('@/components/settings/SettingsModal', () => ({ SettingsModal: () => <div /> }));
vi.mock('@/components/workspace/WorkspaceConnectDialog', () => ({ WorkspaceConnectDialog: () => <div /> }));
vi.mock('@/components/workspace/explorer/IndexExplorer', () => ({ IndexExplorer: () => <div /> }));
vi.mock('@/components/common/BuddyWidget', () => ({ BuddyWidget: () => <div /> }));
vi.mock('@/components/settings/MemoryEditorModal', () => ({ MemoryEditorModal: () => <div /> }));
vi.mock('@/components/settings/MemoryViewerModal', () => ({ MemoryViewerModal: () => <div /> }));
vi.mock('@/components/chat/BtwPopup', () => ({ BtwPopup: () => <div /> }));
vi.mock('@/components/common/Dialog', () => ({ DialogHost: () => <div /> }));
vi.mock('@/components/common/CommandPalette', () => ({ CommandPalette: () => <div /> }));
vi.mock('@/hooks/useDockLiveWatcher', () => ({ useDockLiveWatcher: () => {} }));
vi.mock('@/hooks/useSessionPersistence', () => ({ useSessionPersistence: () => {} }));
vi.mock('@/services/recordingController', () => ({ initRecordingControllerEvents: () => {} }));

// The real WorkspacePanel, with its heavy children stubbed.
vi.mock('@/components/workspace/FileTree', () => ({ FileTree: () => <div data-testid="tree" /> }));
vi.mock('@/components/git/GitChangesPanel', () => ({ GitChangesPanel: () => <div /> }));
vi.mock('@/components/workspace/EditorTabs', () => ({ EditorTabs: () => null }));
vi.mock('@/components/workspace/MonacoEditor', () => ({ MonacoEditor: () => null }));
vi.mock('@/api/workspace', () => ({
  indexStatus: vi.fn().mockResolvedValue({ indexed: false, building: false }),
  queryFile: vi.fn(),
}));

// The real TerminalPanel, with the xterm tab and the APIs stubbed.
vi.mock('@/components/terminal/TerminalTab', async () => {
  const React = await import('react');
  return { TerminalTab: React.forwardRef(() => <div data-testid="term-tab" />) };
});
vi.mock('@/hooks/useTerminal', () => ({ measureCellGrid: () => ({ cols: 80, rows: 24 }) }));
const createTerminalSession = vi.fn().mockResolvedValue({ session_id: 'pty-1', cwd: '/repo' });
const deleteTerminalSession = vi.fn().mockResolvedValue(undefined);
vi.mock('@/api/terminal', () => ({
  createTerminalSession: (...a: unknown[]) => createTerminalSession(...a),
  deleteTerminalSession: (...a: unknown[]) => deleteTerminalSession(...a),
}));
vi.mock('@/api/git', () => ({
  getGitBranch: vi.fn().mockResolvedValue({ branch: 'main' }),
  getGitWorktrees: vi.fn().mockResolvedValue({ worktrees: [] }),
  addGitWorktree: vi.fn(),
  removeGitWorktree: vi.fn(),
  getWorktreeSession: vi.fn().mockResolvedValue({ session: null }),
}));

import AppShell from './AppShell';
import { settleWorkspaceOps } from '@/services/workspaceConnection';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';

type Pending = { url: string; resolve: (r: unknown) => void };
let pending: Pending[] = [];
const fetchMock = vi.fn(
  (url: string) => new Promise((resolve) => pending.push({ url: String(url), resolve })),
);

const json = (body: unknown) => ({
  ok: true,
  status: 200,
  headers: new Headers({ 'content-type': 'application/json' }),
  json: async () => body,
  text: async () => JSON.stringify(body),
});
const statusResp = (path: string | null) => json(path ? { connected: true, path } : { connected: false });
const disconnected = () => json({ disconnected: true, warnings: [] });

const take = (fragment: string) => {
  const i = pending.findIndex((p) => p.url.includes(fragment));
  expect(i, `no pending request for ${fragment}`).toBeGreaterThanOrEqual(0);
  return pending.splice(i, 1)[0];
};
const count = (fragment: string) => pending.filter((p) => p.url.includes(fragment)).length;

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AppShell />
    </QueryClientProvider>,
  );
}

const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

async function mountConnected() {
  const view = renderShell();
  await act(async () => { await flush(); });
  take('/api/workspace/status').resolve(statusResp('/repo'));
  await act(async () => { await flush(); });
  expect(useUIStore.getState().wsConnected).toBe(true);
  return view;
}

beforeEach(async () => {
  for (const p of pending) p.resolve(disconnected());
  await settleWorkspaceOps();
  pending = [];
  fetchMock.mockClear();
  createTerminalSession.mockClear();
  deleteTerminalSession.mockClear();
  for (const key of ['loadConfig', 'loadModels', 'loadDataRetention', 'loadSkills'] as const) {
    useSettingsStore.setState({ [key]: vi.fn().mockResolvedValue(undefined) } as never);
  }
  useSessionStore.setState({ loadSessions: vi.fn().mockResolvedValue(undefined) } as never);
  vi.stubGlobal('fetch', fetchMock);
  useUIStore.setState({
    wsConnected: true,
    wsPath: '/repo',
    workspacePanelCollapsed: false,
    terminalLive: false,
  } as never);
});

describe('AppShell: Disconnect workspace', () => {
  it('hides the workspace on the click, while the request is still in flight', async () => {
    const { getByLabelText, queryByLabelText } = await mountConnected();

    await act(async () => {
      fireEvent.click(getByLabelText('Disconnect workspace'));
    });

    // Nothing has come back from the server yet, and the UI already moved.
    expect(count('/api/workspace/disconnect')).toBe(1);
    expect(useUIStore.getState().wsConnected).toBe(false);
    expect(queryByLabelText('Disconnect workspace')).toBeNull();

    await act(async () => {
      take('/api/workspace/disconnect').resolve(disconnected());
      await flush();
    });
    expect(useUIStore.getState().wsConnected).toBe(false);
  });

  it('a double click sends one request', async () => {
    const { getByLabelText } = await mountConnected();
    const button = getByLabelText('Disconnect workspace');
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
      await flush();
    });
    expect(count('/api/workspace/disconnect')).toBe(1);
  });

  it('a focus resync that was already in flight cannot bring the workspace back', async () => {
    const { getByLabelText, queryByLabelText } = await mountConnected();

    // Clicking into an inactive window: focus (GET /status) then the click.
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      fireEvent.click(getByLabelText('Disconnect workspace'));
      await flush();
    });
    // The server answered the GET before it processed the POST.
    await act(async () => {
      take('/api/workspace/status').resolve(statusResp('/repo'));
      take('/api/workspace/disconnect').resolve(disconnected());
      await flush();
    });

    expect(useUIStore.getState().wsConnected).toBe(false);
    expect(queryByLabelText('Disconnect workspace')).toBeNull();
  });

  it('keeps open terminals alive and the chat panel mounted', async () => {
    const { container, getByLabelText, getByTestId, queryAllByTestId } = await mountConnected();
    const chatNode = getByTestId('chat-panel');

    // The user opens a terminal and runs a dev server in it.
    const bar = container.querySelector('.ws-terminal-toggle-bar') as HTMLElement;
    await act(async () => {
      fireEvent.click(bar);
      await flush();
    });
    expect(createTerminalSession).toHaveBeenCalledTimes(1);
    expect(queryAllByTestId('term-tab')).toHaveLength(1);

    await act(async () => {
      fireEvent.click(getByLabelText('Disconnect workspace'));
      take('/api/workspace/disconnect').resolve(disconnected());
      await flush();
    });

    expect(useUIStore.getState().wsConnected).toBe(false);
    expect(deleteTerminalSession).not.toHaveBeenCalled();
    expect(queryAllByTestId('term-tab')).toHaveLength(1);
    // The running turn's UI is not remounted either.
    expect(getByTestId('chat-panel')).toBe(chatNode);

    // Closing the last terminal by hand is what ends it, as before.
    const close = container.querySelector('.ws-terminal-tab-close') as HTMLElement;
    await act(async () => {
      fireEvent.click(close);
      await flush();
    });
    expect(deleteTerminalSession).toHaveBeenCalledWith('pty-1');
    expect(container.querySelector('.ws-terminal-toggle-bar')).toBeNull();
  });

  it('without a terminal open, the terminal bar goes with the workspace', async () => {
    const { container, getByLabelText } = await mountConnected();
    expect(container.querySelector('.ws-terminal-toggle-bar')).not.toBeNull();
    await act(async () => {
      fireEvent.click(getByLabelText('Disconnect workspace'));
      await flush();
    });
    expect(container.querySelector('.ws-terminal-toggle-bar')).toBeNull();
  });
});
