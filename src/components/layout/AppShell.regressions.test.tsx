/**
 * Opening or closing the right dock must not remount the panels subtree.
 *
 * Reported live: clicking a task card, or closing the Tasks pane with its X,
 * threw the conversation back to the top. Cause was structural, not a scroll
 * handler — AppShell rendered EITHER
 *
 *     <Splitter>{renderPanelsInner()}{dock}</Splitter>
 *
 * or bare `renderPanelsInner()`, chosen by `dockOpen`. That changes the
 * element type at this position, so React unmounts the whole subtree and
 * builds a fresh one; the chat transcript's scroll container comes back as a
 * new DOM node with scrollTop 0. Measured in the browser before the fix:
 * scrollTop 1800 -> 0 on both the open and the close, DOM node replaced each
 * time.
 *
 * This asserts node IDENTITY. A remounted element still passes a
 * presence check but has lost its scroll position, so presence proves
 * nothing here.
 */
import { render } from '@testing-library/react';
import { act } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

// Every heavy child is stubbed: this test is about where ChatPanel sits in
// the tree, not what it renders.
vi.mock('./Sidebar', () => ({ Sidebar: () => <div data-testid="sidebar" /> }));
vi.mock('./Header', () => ({ Header: () => <div data-testid="header" /> }));
vi.mock('./AppStatusBar', () => ({ AppStatusBar: () => <div data-testid="statusbar" /> }));
vi.mock('@/components/chat/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }));
vi.mock('@/components/transcription/TranscriptionPanel', () => ({ TranscriptionPanel: () => <div data-testid="transcript" /> }));
vi.mock('@/components/workspace/WorkspacePanel', () => ({ WorkspacePanel: () => <div data-testid="workspace" /> }));
vi.mock('@/components/terminal/TerminalPanel', () => ({ TerminalPanel: () => <div data-testid="terminal" /> }));
vi.mock('@/components/preview/RightDock', () => ({ RightDock: () => <div data-testid="right-dock" /> }));
vi.mock('@/components/common/ToastContainer', () => ({ ToastContainer: () => <div data-testid="toasts" /> }));
vi.mock('@/components/common/ModelLoadingBanner', () => ({ ModelLoadingBanner: () => <div data-testid="banner" /> }));
vi.mock('@/components/settings/SettingsModal', () => ({ SettingsModal: () => <div data-testid="settings" /> }));
vi.mock('@/components/workspace/WorkspaceConnectDialog', () => ({ WorkspaceConnectDialog: () => <div data-testid="connect" /> }));
vi.mock('@/components/workspace/WorkspaceGraphOverlay', () => ({ WorkspaceGraphOverlay: () => <div data-testid="graph" /> }));
vi.mock('@/components/common/BuddyWidget', () => ({ BuddyWidget: () => <div data-testid="buddy" /> }));
vi.mock('@/components/settings/MemoryEditorModal', () => ({ MemoryEditorModal: () => <div data-testid="mem-edit" /> }));
vi.mock('@/components/settings/MemoryViewerModal', () => ({ MemoryViewerModal: () => <div data-testid="mem-view" /> }));
vi.mock('@/components/chat/BtwPopup', () => ({ BtwPopup: () => <div data-testid="btw" /> }));
vi.mock('@/components/common/Dialog', () => ({ DialogHost: () => <div data-testid="dialogs" /> }));
vi.mock('@/components/common/CommandPalette', () => ({ CommandPalette: () => <div data-testid="palette" /> }));
vi.mock('@/hooks/useDockLiveWatcher', () => ({ useDockLiveWatcher: () => {} }));
vi.mock('@/hooks/useSessionPersistence', () => ({ useSessionPersistence: () => {} }));
vi.mock('@/services/recordingController', () => ({ initRecordingControllerEvents: () => {} }));

import AppShell from './AppShell';
import { useDockStore } from '@/stores/dockStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';

beforeEach(() => {
  useDockStore.setState({ panels: [], sizes: [], open: false });
  // AppShell's mount effects call these; stub them so the render is inert.
  for (const key of ['loadConfig', 'loadModels', 'loadDataRetention', 'loadSkills', 'loadMCP'] as const) {
    useSettingsStore.setState({ [key]: vi.fn().mockResolvedValue(undefined) } as never);
  }
  useSessionStore.setState({ loadSessions: vi.fn().mockResolvedValue(undefined) } as never);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ connected: false }) }));
});

const openTasks = () =>
  act(() => {
    useDockStore.getState().openPanel({ id: 'tasks', kind: 'tasks', title: 'Tasks' });
  });
const closeTasks = () =>
  act(() => {
    useDockStore.getState().closePanel('tasks');
  });

describe('AppShell — right dock open/close', () => {
  it('keeps the chat panel on the same DOM node across an open and a close', () => {
    const { getByTestId } = render(<AppShell />);
    const original = getByTestId('chat-panel');

    openTasks();
    expect(getByTestId('right-dock')).toBeTruthy(); // the dock really did open
    expect(getByTestId('chat-panel')).toBe(original);

    closeTasks();
    expect(useDockStore.getState().open).toBe(false);
    expect(getByTestId('chat-panel')).toBe(original);
  });

  it('survives repeated toggling without ever rebuilding the chat panel', () => {
    const { getByTestId } = render(<AppShell />);
    const original = getByTestId('chat-panel');
    for (let i = 0; i < 3; i++) {
      openTasks();
      closeTasks();
    }
    expect(getByTestId('chat-panel')).toBe(original);
  });
});

/**
 * The workspace panel must come back by itself after a backend blip.
 *
 * Reported live as "the workspace disappears for no reason", then "I open it
 * but it still says not connected". The recovery fetch on mount was a single
 * request whose failure was swallowed, so any moment the backend was
 * unreachable — a restart, sleep/wake, the app relaunching — left the panel
 * gone from the UI with no path back except reconnecting by hand, while the
 * server still had the workspace connected the whole time. Confirmed against
 * a real server: backend up and connected, frontend wsConnected false, panel
 * absent from the DOM, permanently.
 */
describe('AppShell — workspace status recovery', () => {
  const status = (path: string | null) => ({
    ok: true,
    json: async () => (path ? { connected: true, path } : { connected: false }),
  });

  beforeEach(() => {
    useUIStore.setState({ wsConnected: false, wsPath: '' });
  });

  it('retries after a failed status call and restores the workspace', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('Load failed')) // backend down
      .mockResolvedValue(status('/repo')); // back up
    vi.stubGlobal('fetch', fetchMock);

    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });
    expect(useUIStore.getState().wsConnected).toBe(false); // still down

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(useUIStore.getState().wsConnected).toBe(true);
    expect(useUIStore.getState().wsPath).toBe('/repo');
    vi.useRealTimers();
  });

  it('stops retrying once the server answers cleanly', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(status(null));
    vi.stubGlobal('fetch', fetchMock);

    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });
    const callsAfterFirstAnswer = fetchMock.mock.calls.length;

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    // "not connected" is an answer, not a failure — no retry storm.
    expect(fetchMock.mock.calls.length).toBe(callsAfterFirstAnswer);
    vi.useRealTimers();
  });

  it('re-asks when the window regains focus, for outages past the backoff', async () => {
    const fetchMock = vi.fn().mockResolvedValue(status(null));
    vi.stubGlobal('fetch', fetchMock);
    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });

    fetchMock.mockResolvedValue(status('/repo')); // workspace came back
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      await Promise.resolve();
    });
    expect(useUIStore.getState().wsConnected).toBe(true);
    expect(useUIStore.getState().wsPath).toBe('/repo');
  });

  /**
   * The other direction, and the one that cost a session: the sync only ever
   * set `true`, so once the server lost the workspace (a second backend
   * sharing WHISPER_HOME writing workspace_config.json, a stray /disconnect)
   * the stale `true` survived every reload and focus event. The panel kept
   * rendering a tree the server no longer had, while each turn assembled its
   * tool pool with ws_connected=False and dropped the git and GitHub tools.
   */
  it('clears a stale connection when the server answers that it has none', async () => {
    useUIStore.setState({ wsConnected: true, wsPath: '/repo' }); // stale
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(status(null)));

    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });

    expect(useUIStore.getState().wsConnected).toBe(false);
    expect(useUIStore.getState().wsPath).toBe('');
  });

  it('clears a stale connection on focus, not just on mount', async () => {
    const fetchMock = vi.fn().mockResolvedValue(status('/repo'));
    vi.stubGlobal('fetch', fetchMock);
    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });
    expect(useUIStore.getState().wsConnected).toBe(true);

    fetchMock.mockResolvedValue(status(null)); // workspace went away
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      await Promise.resolve();
    });
    expect(useUIStore.getState().wsConnected).toBe(false);
  });

  /**
   * Guards the fix above from undoing PR #71: an UNREACHABLE backend is not an
   * answer, so it must never clear a live workspace. Only a clean response
   * saying connected:false may.
   */
  it('keeps the workspace while the backend is unreachable', async () => {
    vi.useFakeTimers();
    useUIStore.setState({ wsConnected: true, wsPath: '/repo' });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Load failed')));

    render(<AppShell />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(useUIStore.getState().wsConnected).toBe(true);
    expect(useUIStore.getState().wsPath).toBe('/repo');
    vi.useRealTimers();
  });
});
