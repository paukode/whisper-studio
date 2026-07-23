import React, { useCallback, useEffect } from 'react';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useToolStore } from '@/stores/toolStore';
import { useLayoutStore } from '@/stores/layoutStore';
import { useDockStore } from '@/stores/dockStore';
import { useDockLiveWatcher } from '@/hooks/useDockLiveWatcher';
import { useKeyboardShortcut } from '@/hooks/useKeyboardShortcut';
import { useSessionPersistence } from '@/hooks/useSessionPersistence';
import { killSessionStream } from '@/hooks/chatStream/streamControl';
import { useActiveChatStore } from '@/stores/sessionRuntimes';
import { useSubagentStore } from '@/stores/subagentStore';
import { initRecordingControllerEvents } from '@/services/recordingController';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { AppStatusBar } from './AppStatusBar';
import { Splitter } from './Splitter';
import { ToastContainer } from '@/components/common/ToastContainer';
import { ModelLoadingBanner } from '@/components/common/ModelLoadingBanner';
import { SettingsModal } from '@/components/settings/SettingsModal';
import { TranscriptionPanel } from '@/components/transcription/TranscriptionPanel';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { WorkspaceConnectDialog } from '@/components/workspace/WorkspaceConnectDialog';
import { WorkspaceGraphOverlay } from '@/components/workspace/WorkspaceGraphOverlay';
import { WorkspacePanel } from '@/components/workspace/WorkspacePanel';
import { TerminalPanel } from '@/components/terminal/TerminalPanel';
import { RightDock } from '@/components/preview/RightDock';
import { BuddyWidget } from '@/components/common/BuddyWidget';
import { MemoryEditorModal } from '@/components/settings/MemoryEditorModal';
import { MemoryViewerModal } from '@/components/settings/MemoryViewerModal';
import { ErrorBoundary } from '@/components/common/ErrorBoundary';
import { BtwPopup } from '@/components/chat/BtwPopup';
import { DialogHost } from '@/components/common/Dialog';
import { CommandPalette } from '@/components/common/CommandPalette';

/**
 * AppShell — top-level layout matching the vanilla templates/index.html structure.
 *
 * Outputs:
 *   div.app
 *     aside.sidebar.collapsed
 *     div.workspace
 *       header.header
 *       div.panels
 *         div.ws-expand-tab#wsExpandTab        (visible when workspace collapsed)
 *         div.panel.workspace-panel#workspacePanel
 *         div.resize-handle.ws-resize-handle#wsResizeHandle
 *         div.panels-right-col
 *           div#transcriptPanel
 *           div.resize-handle#resizeHandle
 *           div#chatPanelWrap
 */
const AppShell: React.FC = () => {
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);
  const transcriptVisible = useUIStore((s) => s.transcriptVisible);
  const wsConnected = useUIStore((s) => s.wsConnected);
  const workspacePanelCollapsed = useUIStore((s) => s.workspacePanelCollapsed);
  const loadConfig = useSettingsStore((s) => s.loadConfig);
  const loadModels = useSettingsStore((s) => s.loadModels);
  const loadDataRetention = useSettingsStore((s) => s.loadDataRetention);
  const loadSkills = useSettingsStore((s) => s.loadSkills);
  const loadMCP = useSettingsStore((s) => s.loadMCP);
  const loadSessions = useSessionStore((s) => s.loadSessions);

  // Track whether editor tabs are open (for ws-ide-open class)
  const hasEditorTabs = useWorkspaceStore((s) => s.editorTabs.length > 0);

  // Memory editor modal state
  const memoryEditorOpen = useUIStore((s) => s.memoryEditorOpen);
  const memoryViewerOpen = useUIStore((s) => s.memoryViewerOpen);

  // /btw popup state
  const btwPopup = useUIStore((s) => s.btwPopup);

  // ── Layout split fractions (single source of truth, persisted) ──
  // No refs, no DOM mutation, no drag math here. The Splitter components
  // own all of that and drive layout entirely from these fractions.
  const workspaceFrac = useLayoutStore((s) => s.workspaceFrac);
  const transcriptFrac = useLayoutStore((s) => s.transcriptFrac);
  const setWorkspaceFrac = useLayoutStore((s) => s.setWorkspaceFrac);
  const setTranscriptFrac = useLayoutStore((s) => s.setTranscriptFrac);

  // ── Right-side dock (live preview / plan / file / tasks) ──
  const dockFrac = useLayoutStore((s) => s.dockFrac);
  const setDockFrac = useLayoutStore((s) => s.setDockFrac);
  const dockOpen = useDockStore((s) => s.open);
  // Reopen chip: a preview session is running but its live panel is closed.
  const liveSession = useDockStore((s) => s.liveSession);
  const liveClosed = useDockStore((s) => !s.panels.some((p) => p.kind === 'live'));
  const openLive = useDockStore((s) => s.openLive);
  // Reflect a running preview session as a live dock panel (open/close).
  useDockLiveWatcher();

  // Wire the recording controller's window events once (start/stop from
  // welcome cards, live engine switch, participant hint).
  useEffect(() => {
    initRecordingControllerEvents();
  }, []);

  // Load real config on startup
  useEffect(() => {
    void loadConfig();
    // Load the model list only — do NOT eager-load on-device weights at startup.
    // In local (or hybrid) mode the user loads a model when they start a session:
    // selecting it loads it behind the banner, and the send path loads it on the
    // first message if needed. Cloud models are API calls and need no loading.
    // This keeps startup fast and memory free until a model is actually wanted.
    void loadModels();
    void loadDataRetention();
    void loadSkills();
    void loadMCP();
    // After loadSessions, restore the persisted current session id if any.
    // This survives the page-reload-mid-chat case where Vite HMR cascades on
    // a workspace-wide file change (assistant ran `git checkout`/`merge`) and
    // forces a full reload through a non-HMR-able module.
    void (async () => {
      const persistedId = useSessionStore.getState().currentSessionId;
      await loadSessions();
      if (persistedId) {
        const exists = useSessionStore.getState().sessions.some((s) => s.id === persistedId);
        if (exists) {
          // Reset id to null first so switchSession doesn't early-return
          // on the "same id" guard — we need it to actually fetch the
          // session body, not just no-op.
          useSessionStore.setState({ currentSessionId: null });
          await useSessionStore.getState().switchSession(persistedId);
        } else {
          // Stale persisted id — drop it so the next session prompt is fresh.
          useSessionStore.setState({ currentSessionId: null });
        }
      }
    })();
    // Populate the global tool store so the chat autocomplete has
    // skills + MCP tools available for filtering.
    void useToolStore.getState().fetchSkills();
    void useToolStore.getState().fetchMCPTools();
  }, [loadConfig, loadModels, loadDataRetention, loadSkills, loadMCP, loadSessions]);

  // Check if workspace was already connected (page refresh recovery)
  useEffect(() => {
    fetch('/api/workspace/status')
      .then((r) => r.ok ? r.json() : null)
      .then((data: { connected?: boolean; path?: string } | null) => {
        if (data?.connected && data.path) {
          useUIStore.getState().setWsConnected(true, data.path);
        }
      })
      .catch(() => { /* ignore */ });
  }, []);

  // Auto-save is owned by the session runtime registry now: every live
  // session's stores save themselves on change (stream end, message
  // append, segment growth) whether that session is on screen or not.
  // This hook only adds the cross-runtime safety nets: periodic 30s
  // saves and the beforeunload beacon per live session.
  useSessionPersistence();

  // Global keyboard shortcuts. Using `mod+` so the same binding works on
  // macOS (Cmd) and Linux/Windows (Ctrl).
  useKeyboardShortcut('mod+,', () => {
    useUIStore.getState().openSettings();
  });
  useKeyboardShortcut('mod+b', () => {
    useUIStore.getState().toggleSidebar();
  });
  useKeyboardShortcut('mod+k', () => {
    useUIStore.getState().openCommandPalette();
  });
  // ESC = instant kill switch for the viewed session's stream + all running
  // subagents. Armed only while something is streaming, so ESC keeps its
  // normal meaning everywhere else; overlay dismissers that consume ESC
  // (preventDefault) always win — the next ESC then stops the stream.
  const isActiveStreaming = useActiveChatStore((s) => s.isStreaming);
  const hasRunningSubagents = useSubagentStore((s) => Object.keys(s.stops).length > 0);
  useKeyboardShortcut(
    'escape',
    () => killSessionStream(useSessionStore.getState().currentSessionId),
    isActiveStreaming || hasRunningSubagents,
  );

  // ── Workspace collapse / expand ──
  const handleCollapseWorkspace = useCallback(() => {
    useUIStore.getState().collapseWorkspacePanel();
  }, []);

  const handleExpandWorkspace = useCallback(() => {
    useUIStore.getState().expandWorkspacePanel();
  }, []);

  // Render the right-column (transcript + chat top row, plus terminal below).
  // Extracted so it can be slotted with or without the workspace splitter
  // wrapping it — depending on whether the workspace pane is visible.
  const renderRightColumn = () => (
    <div
      className="panels-right-col"
      style={{ display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0, minWidth: 0 }}
    >
      {transcriptVisible ? (
        // Layout: [chat][transcript].
        //
        // The transcript sits on the RIGHT — slot 0 is chat, slot 1
        // is transcript. ``transcriptFrac`` stays defined as "fraction
        // taken by the transcript pane" (same semantics as before),
        // so the persisted user preference doesn't reset when the
        // panes swap sides. The ratio passed to the Splitter is the
        // fraction of slot 0 (chat) = ``1 - transcriptFrac``; the
        // ``onChange`` flips it back.
        <Splitter
          direction="horizontal"
          ratio={1 - transcriptFrac}
          onChange={(r) => setTranscriptFrac(1 - r)}
          // Floor keeps the chat composer + header visible when the terminal
          // below is dragged tall or the window is shrunk (matches MIN_CHAT_PX
          // in TerminalPanel.tsx). The terminal is flex-shrinkable, so it
          // yields to this minimum instead of clipping the composer.
          style={{ flex: '1 1 auto', minHeight: 220 }}
        >
          <ChatPanel />
          <TranscriptionPanel hidden={false} />
        </Splitter>
      ) : (
        <div style={{ display: 'flex', flex: '1 1 auto', minHeight: 220, minWidth: 0, overflow: 'hidden' }}>
          <ChatPanel />
        </div>
      )}

      {/* Terminal panel — below, shown when workspace connected */}
      {wsConnected && (
        <ErrorBoundary label="Terminal">
          <TerminalPanel />
        </ErrorBoundary>
      )}
    </div>
  );

  // The app content (workspace + chat/transcript area) as it existed before
  // the dock — extracted so it can be the left pane of the chat|dock Splitter
  // or fill .panels directly when the dock is closed.
  const renderPanelsInner = () =>
    wsConnected && !workspacePanelCollapsed ? (
      <Splitter
        direction="horizontal"
        ratio={workspaceFrac}
        onChange={setWorkspaceFrac}
        handleClassName="ws-resize-handle"
        style={{ flex: '1 1 auto' }}
      >
        <div className={`panel workspace-panel${hasEditorTabs ? ' ws-ide-open' : ''}`} id="workspacePanel">
          <ErrorBoundary label="Workspace">
            <WorkspacePanel onCollapse={handleCollapseWorkspace} />
          </ErrorBoundary>
        </div>
        {renderRightColumn()}
      </Splitter>
    ) : (
      renderRightColumn()
    );

  return (
    <div className="app">
      {/* Sessions Sidebar */}
      <Sidebar collapsed={sidebarCollapsed} />

      {/* Mobile drawer scrim. Below ~900px the sidebar floats over the app as
        * a drawer instead of taking a fixed column; this dims and captures
        * taps on the rest of the UI so tapping outside closes it. Hidden on
        * desktop via CSS (.sidebar-scrim is display:none until the mobile
        * breakpoint), so rendering it whenever the sidebar is open is a no-op
        * on wide viewports. */}
      {!sidebarCollapsed && (
        <div
          className="sidebar-scrim"
          onClick={() => useUIStore.getState().toggleSidebar()}
          aria-hidden="true"
        />
      )}

      {/* Main workspace */}
      <div className="workspace">
        <Header />

        {/* Panels area.
            Layout shape:
              workspace visible:   <Splitter horizontal>[workspace] [rightCol]</Splitter>
              workspace hidden:    rightCol fills .panels directly
              rightCol (column):   <Splitter horizontal>[chat] [transcript]</Splitter> (or just chat) + optional terminal
            Every visible split is a ratio-flex Splitter; no pane ever has a pixel-pinned width. Open/close
            any pane and surviving panes redistribute via flexbox — no JS reconciliation. */}
        <div className="panels" id="panels">
          {/* Workspace expand tab — visible when workspace connected but panel collapsed */}
          {wsConnected && workspacePanelCollapsed && (
            <div
              className="ws-expand-tab"
              id="wsExpandTab"
              title="Show workspace"
              onClick={handleExpandWorkspace}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              </svg>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <polyline points="9 18 15 12 9 6"/>
              </svg>
            </div>
          )}

          {dockOpen ? (
            <Splitter
              direction="horizontal"
              ratio={1 - dockFrac}
              onChange={(r) => setDockFrac(1 - r)}
              firstMinPx={420}
              secondMinPx={320}
              style={{ flex: '1 1 auto' }}
            >
              {renderPanelsInner()}
              <ErrorBoundary label="RightDock">
                <RightDock />
              </ErrorBoundary>
            </Splitter>
          ) : (
            renderPanelsInner()
          )}
        </div>

        {/* Persistent status strip — model, effort, git, context, tokens,
          * background tasks. Always mounted so it's glanceable across panels. */}
        <AppStatusBar />
      </div>

      {/* Overlay components */}
      <ModelLoadingBanner />
      <ToastContainer />
      <SettingsModal />
      <WorkspaceConnectDialog />
      <WorkspaceGraphOverlay />
      <BuddyWidget />
      <MemoryEditorModal isOpen={memoryEditorOpen} onClose={() => useUIStore.getState().closeMemoryEditor()} />
      <MemoryViewerModal isOpen={memoryViewerOpen} onClose={() => useUIStore.getState().closeMemoryViewer()} />
      <DialogHost />
      <CommandPalette />

      {/* Reopen chip — a preview server is running but its live panel is closed. */}
      {liveSession && liveClosed && (
        <button
          type="button"
          onClick={openLive}
          title="Show the live preview"
          style={{
            position: 'fixed', left: 16, bottom: 16, zIndex: 50,
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '8px 12px', borderRadius: 999,
            background: 'var(--bg-secondary, #222)', color: 'var(--text-primary, #fff)',
            border: '1px solid var(--border, #444)', cursor: 'pointer',
            boxShadow: '0 4px 14px rgba(0,0,0,0.25)', fontSize: 13,
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: 999, background: 'var(--accent-live, #e5484d)' }} />
          Live preview
        </button>
      )}

      {/* /btw popup */}
      {btwPopup && (
        <BtwPopup
          question={btwPopup.question}
          answer={btwPopup.answer}
          onClose={() => useUIStore.getState().setBtwPopup(null)}
        />
      )}

    </div>
  );
};

export default AppShell;
