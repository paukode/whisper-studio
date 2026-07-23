import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useUIStore, dialogConfirm } from '@/stores/uiStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { get, post } from '@/api/client';
import {
  indexStatus,
  buildIndex,
  removeIndex,
  cancelIndex,
  getIndexSettings,
  updateIndexSettings,
  getIndexAgent,
  removeRecentWorkspace,
  clearUnindexedRecents,
  type IndexStatus,
  type IndexSettings,
} from '@/api/workspace';
import { localModelDownloaded, downloadLocalModel } from '@/api/localModel';
import { useRecentWorkspaces } from '@/hooks/useRecentWorkspaces';
import { useResizableDialog } from '@/hooks/useResizableDialog';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import { IndexSettingsPanel } from './IndexSettingsPanel';
import { RecentWorkspaceItem } from './RecentWorkspaceItem';
import { WorkspaceBrowser } from './WorkspaceBrowser';
import {
  LOCAL_RELATIONS_MODEL,
  normalizeWsPath,
  type BrowseEntry,
  type BrowseResponse,
  type SortMode,
} from './workspaceConnectHelpers';

/**
 * Workspace connect dialog overlay matching the vanilla HTML structure.
 * Includes folder browser and recent workspaces.
 */
export const WorkspaceConnectDialog: React.FC = () => {
  const isOpen = useUIStore((s) => s.workspaceConnectOpen);
  const closeDialog = useUIStore((s) => s.closeWorkspaceConnect);

  const [path, setPath] = useState('');
  const [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [recentFilter, setRecentFilter] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  // Resizable + movable popup (size/position persisted to localStorage).
  const {
    dialogRef,
    style: dialogStyle,
    onMoveStart,
    onResizeStart,
    reset: resetDialogSize,
  } = useResizableDialog(STORAGE_KEYS.WS_DIALOG_GEOMETRY, { defaultW: 480, minW: 320, minH: 260 });

  // Browser state
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserCurrent, setBrowserCurrent] = useState('');
  const [browserParent, setBrowserParent] = useState<string | null>(null);
  const [browserEntries, setBrowserEntries] = useState<BrowseEntry[]>([]);
  const [browserFiles, setBrowserFiles] = useState<BrowseEntry[]>([]);
  const [browserFileTotal, setBrowserFileTotal] = useState(0);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>('name-asc');

  const sortList = React.useCallback(
    (list: BrowseEntry[]) => {
      const out = [...list];
      switch (sortMode) {
        case 'name-desc':
          return out.sort((a, b) => b.name.localeCompare(a.name));
        case 'mtime-desc':
          return out.sort((a, b) => b.mtime - a.mtime);
        case 'mtime-asc':
          return out.sort((a, b) => a.mtime - b.mtime);
        default:
          return out.sort((a, b) => a.name.localeCompare(b.name));
      }
    },
    [sortMode],
  );
  const sortedEntries = React.useMemo(() => sortList(browserEntries), [browserEntries, sortList]);
  const sortedFiles = React.useMemo(() => sortList(browserFiles), [browserFiles, sortList]);

  // Recent workspaces — shared hook (same react-query key + shape as the
  // toolbar Workspace dropdown).
  const recentWorkspaces = useRecentWorkspaces(isOpen);
  const queryClient = useQueryClient();

  // ── Index state: per-path status, the connect-time toggle, and the daily
  // refresh schedule. Statuses are polled while any index is building.
  const [indexOnConnect, setIndexOnConnect] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, IndexStatus>>({});
  // Paths whose Stop was clicked but whose build hasn't wound down yet. Cancel
  // is cooperative (the build only checks between files), so there's a lag —
  // we show "Stopping…" immediately so the click visibly registers, then clear
  // the flag once a status poll reports the build is no longer running.
  const [stopping, setStopping] = useState<Record<string, boolean>>({});
  // Per-folder settings (loaded lazily when a folder's ⋯ menu opens), the open
  // menu's path, and whether the background helper is supported on this platform.
  const [settingsByPath, setSettingsByPath] = useState<Record<string, IndexSettings>>({});
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [agentSupported, setAgentSupported] = useState(false);
  const statusesRef = useRef(statuses);
  useEffect(() => {
    statusesRef.current = statuses;
  }, [statuses]);

  // Drop the "stopping" flag for any path the latest statuses show as no longer
  // building (the cancel landed, or the build finished on its own).
  const clearStoppedFlags = useCallback(
    (paths: string[], results: PromiseSettledResult<IndexStatus>[]) => {
      setStopping((prev) => {
        if (Object.keys(prev).length === 0) return prev;
        let changed = false;
        const next = { ...prev };
        results.forEach((r, i) => {
          const p = paths[i];
          if (r.status === 'fulfilled' && !r.value.building && next[p]) {
            delete next[p];
            changed = true;
          }
        });
        return changed ? next : prev;
      });
    },
    [],
  );

  const refreshStatuses = useCallback(
    async (wsPaths: string[]) => {
      const results = await Promise.allSettled(wsPaths.map((p) => indexStatus(p)));
      setStatuses((prev) => {
        const next = { ...prev };
        results.forEach((r, i) => {
          if (r.status === 'fulfilled') next[wsPaths[i]] = r.value;
        });
        return next;
      });
      clearStoppedFlags(wsPaths, results);
    },
    [clearStoppedFlags],
  );

  // Is the background helper supported here (macOS)? Once per open.
  useEffect(() => {
    if (!isOpen) return;
    getIndexAgent()
      .then((a) => setAgentSupported(a.supported))
      .catch(() => {});
  }, [isOpen]);

  // For a freshly-picked folder (not yet in Recent), load its pending index
  // settings when "index on connect" is ticked, so the inline steps below the
  // toggle reflect — and persist to — the same per-folder store the ⋯ menu uses.
  useEffect(() => {
    const p = normalizeWsPath(path);
    if (!isOpen || !indexOnConnect || !p) return;
    if (recentWorkspaces.includes(p) || settingsByPath[p]) return;
    getIndexSettings(p)
      .then((s) => setSettingsByPath((prev) => ({ ...prev, [p]: s })))
      .catch(() => {});
  }, [isOpen, indexOnConnect, path, recentWorkspaces, settingsByPath]);

  // Open a folder's ⋯ menu, lazily loading its settings. Safe for any state:
  // a not-yet-indexed folder reads the pending pre-index store server-side (no
  // empty index is created), so you can configure it before/while it indexes.
  const toggleMenu = useCallback(
    (wsPath: string) => {
      setOpenMenu((cur) => {
        const next = cur === wsPath ? null : wsPath;
        if (next && !settingsByPath[next]) {
          getIndexSettings(next)
            .then((s) => setSettingsByPath((prev) => ({ ...prev, [next]: s })))
            .catch(() => {});
        }
        return next;
      });
    },
    [settingsByPath],
  );

  // Patch one folder's settings and keep the local copy in sync with the server.
  const updateOne = useCallback(
    (wsPath: string, patch: Parameters<typeof updateIndexSettings>[1]) => {
      updateIndexSettings(wsPath, patch)
        .then((s) => setSettingsByPath((prev) => ({ ...prev, [wsPath]: s })))
        .catch(() => {});
    },
    [],
  );

  // Choosing the on-device engine: make sure Gemma is downloaded first (behind
  // the shared banner, which offers Cancel). On cancel/failure, fall back to
  // "none" so the folder simply has no engine selected yet.
  const handleEngineChange = useCallback(
    async (wsPath: string, value: IndexSettings['typed_relations']['engine']) => {
      if (value !== 'local') {
        updateOne(wsPath, { typed_relations: { engine: value } });
        return;
      }
      if (await localModelDownloaded(LOCAL_RELATIONS_MODEL)) {
        updateOne(wsPath, { typed_relations: { engine: 'local' } });
        return;
      }
      const outcome = await downloadLocalModel(LOCAL_RELATIONS_MODEL, 'Gemma 4 12B (Local)');
      updateOne(wsPath, { typed_relations: { engine: outcome === 'ready' ? 'local' : 'none' } });
    },
    [updateOne],
  );

  // Fetch statuses for the recent list on open; poll every 2s while building.
  // Inlined (rather than calling refreshStatuses) so the setState lands after
  // the await — a data-fetch subscription, not a synchronous effect setState.
  useEffect(() => {
    if (!isOpen || recentWorkspaces.length === 0) return;
    let active = true;
    const poll = async (paths: string[]) => {
      if (paths.length === 0) return;
      const results = await Promise.allSettled(paths.map((p) => indexStatus(p)));
      if (!active) return;
      setStatuses((prev) => {
        const next = { ...prev };
        results.forEach((r, i) => {
          if (r.status === 'fulfilled') next[paths[i]] = r.value;
        });
        return next;
      });
      clearStoppedFlags(paths, results);
    };
    // Initial open: one status fetch per recent folder to populate the badges.
    void poll(recentWorkspaces);
    // Thereafter only re-poll folders that are actually building. Polling every
    // recent workspace each tick spammed the server log with status calls for
    // folders that weren't doing anything; a single build now polls just itself.
    const id = setInterval(() => {
      const buildingPaths = recentWorkspaces.filter((p) => statusesRef.current[p]?.building);
      void poll(buildingPaths);
    }, 2000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [isOpen, recentWorkspaces, clearStoppedFlags]);

  const handleReindex = useCallback(
    async (e: React.MouseEvent, wsPath: string) => {
      e.stopPropagation();
      // Optimistic: flip to building so the badge updates immediately.
      setStatuses((prev) => ({
        ...prev,
        [wsPath]: { ...(prev[wsPath] ?? { indexed: false }), building: true },
      }));
      try {
        await buildIndex(wsPath);
      } catch {
        /* surfaced via polled status */
      }
      void refreshStatuses([wsPath]);
    },
    [refreshStatuses],
  );

  const handleRemoveIndex = useCallback(async (e: React.MouseEvent, wsPath: string) => {
    e.stopPropagation();
    try {
      await removeIndex(wsPath);
    } catch {
      /* ignore */
    }
    setStatuses((prev) => ({ ...prev, [wsPath]: { indexed: false, building: false } }));
  }, []);

  // Forget a not-indexed recent (indexed ones are protected server-side too).
  const handleRemoveRecent = useCallback(
    async (e: React.MouseEvent, wsPath: string) => {
      e.stopPropagation();
      try {
        await removeRecentWorkspace(wsPath);
      } catch {
        /* ignore */
      }
      void queryClient.invalidateQueries({ queryKey: ['workspace-recent'] });
    },
    [queryClient],
  );

  const handleClearUnindexed = useCallback(async () => {
    try {
      await clearUnindexedRecents();
    } catch {
      /* ignore */
    }
    void queryClient.invalidateQueries({ queryKey: ['workspace-recent'] });
  }, [queryClient]);

  const handleStopIndex = useCallback(
    async (e: React.MouseEvent, wsPath: string) => {
      e.stopPropagation();
      // Immediate feedback: the cancel is cooperative and can lag (the build only
      // checks between files, and the first file pays the model-load cost), so
      // flip to "Stopping…" now rather than waiting for the next 2s poll.
      setStopping((prev) => ({ ...prev, [wsPath]: true }));
      try {
        await cancelIndex(wsPath);
      } catch {
        /* ignore */
      }
      // The build stops after the current file; the poll clears both flags.
      void refreshStatuses([wsPath]);
    },
    [refreshStatuses],
  );

  // Browse a directory
  const browseTo = useCallback(async (dirPath: string) => {
    setBrowserLoading(true);
    try {
      const data = await get<BrowseResponse>(
        `/api/workspace/browse?path=${encodeURIComponent(dirPath)}`,
      );
      setBrowserCurrent(data.current);
      setBrowserParent(data.parent ?? null);
      // Prefer the rich entries shape; fall back to flat names from older
      // backends (mtime 0 renders without a date).
      const entries = Array.isArray(data.entries)
        ? data.entries
        : (Array.isArray(data.dirs) ? data.dirs : []).map((name) => ({ name, mtime: 0 }));
      setBrowserEntries(entries);
      setBrowserFiles(Array.isArray(data.files) ? data.files : []);
      setBrowserFileTotal(
        typeof data.file_total === 'number' ? data.file_total : (data.files?.length ?? 0),
      );
      setPath(data.current);
      setBrowserOpen(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Browse failed';
      setError(message);
    } finally {
      setBrowserLoading(false);
    }
  }, []);

  // Reset transient form state each time the dialog opens. During-render
  // previous-value pattern instead of setState-in-effect.
  const [prevOpen, setPrevOpen] = useState(isOpen);
  if (isOpen !== prevOpen) {
    setPrevOpen(isOpen);
    if (isOpen) {
      setPath('');
      setError('');
      setConnecting(false);
      setRecentFilter('');
      setBrowserOpen(false);
      setBrowserEntries([]);
      setBrowserFiles([]);
      setBrowserFileTotal(0);
      setIndexOnConnect(false);
      // The dialog never unmounts (it just renders null when closed), so these
      // survive a close. Collapse any open ⋯ menu and drop cached per-folder
      // settings so each fresh open re-reads current server state.
      setOpenMenu(null);
      setSettingsByPath({});
    }
  }

  // Focus the input when the dialog opens (DOM side effect — fine in an effect).
  useEffect(() => {
    if (!isOpen) return;
    const t = setTimeout(() => inputRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [isOpen]);

  // Close on Escape
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeDialog();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, closeDialog]);

  // Only dismiss on a genuine backdrop click. A resize/move drag that starts
  // inside the dialog and releases over the backdrop fires a click whose target
  // is the overlay too; without this guard that closed the popup on every
  // resize. Close only when the press ALSO started on the backdrop.
  const overlayMouseDownRef = useRef(false);
  const handleOverlayPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    overlayMouseDownRef.current = e.target === e.currentTarget;
  }, []);
  const handleOverlayClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const startedOnBackdrop = overlayMouseDownRef.current;
      overlayMouseDownRef.current = false;
      if (startedOnBackdrop && e.target === e.currentTarget) {
        closeDialog();
      }
    },
    [closeDialog],
  );

  const connectTo = useCallback(
    async (targetPath: string) => {
      const trimmed = targetPath.trim();
      if (!trimmed) {
        setError('Please enter a workspace path.');
        return;
      }

      setError('');

      // Connecting a new workspace mass-closes every open editor tab (below,
      // via the silent closeTab loop). Guard unsaved edits with ONE batched
      // prompt up front — a per-tab prompt would be unbearable here. If the
      // user declines, abort before we touch the backend.
      const dirtyCount = useWorkspaceStore
        .getState()
        .editorTabs.filter((t) => t.isDirty).length;
      if (dirtyCount > 0) {
        const ok = await dialogConfirm({
          title: 'Discard unsaved changes?',
          message: `You have unsaved changes in ${dirtyCount} file${dirtyCount === 1 ? '' : 's'}. Connecting a new workspace will discard them. Continue?`,
          danger: true,
          confirmText: 'Continue',
        });
        if (ok !== true) return;
      }

      setConnecting(true);

      try {
        await post('/api/workspace/connect', { path: trimmed });
        // Clear old editor tabs from previous workspace
        const ws = useWorkspaceStore.getState();
        for (const tab of ws.editorTabs) {
          ws.closeTab(tab.path);
        }
        useUIStore.getState().setWsConnected(true, trimmed);
        // Kick off indexing in the background if requested; it continues
        // server-side after the dialog closes.
        if (indexOnConnect) {
          buildIndex(trimmed).catch(() => {});
        }
        closeDialog();
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Connection failed';
        setError(message);
      } finally {
        setConnecting(false);
      }
    },
    [closeDialog, indexOnConnect],
  );

  const handleConnect = useCallback(() => {
    void connectTo(path);
  }, [path, connectTo]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        void handleConnect();
      }
    },
    [handleConnect],
  );

  const handleBrowse = useCallback(() => {
    // Default to the user's home directory — the backend expands '~'.
    const startPath = path.trim() || '~';
    void browseTo(startPath);
  }, [path, browseTo]);

  // Native OS folder picker as a secondary option. The in-page browser is
  // the canonical flow (recents + sorting); this is for users who prefer
  // the system dialog. Silently no-ops if the backend can't open one.
  const handleNativePick = useCallback(async () => {
    try {
      const data = await get<{ path: string | null; cancelled?: boolean }>(
        '/api/workspace/pick-folder',
      );
      if (data.path) void connectTo(data.path);
    } catch {
      setError('Native folder picker is not available here. Use Browse instead.');
    }
  }, [connectTo]);

  const handleBrowseDir = useCallback(
    (dir: string) => {
      const newPath = browserCurrent === '/' ? '/' + dir : browserCurrent + '/' + dir;
      void browseTo(newPath);
    },
    [browserCurrent, browseTo],
  );

  const handleBrowseUp = useCallback(() => {
    if (browserParent) {
      void browseTo(browserParent);
    }
  }, [browserParent, browseTo]);

  const handleSelectRecent = useCallback(
    (wsPath: string) => {
      setPath(wsPath);
      setBrowserOpen(false);
      // Auto-connect like vanilla JS (wsConnectConfirm.click())
      void connectTo(wsPath);
    },
    [connectTo],
  );

  if (!isOpen) return null;

  // Recents that can be forgotten (indexed folders are kept). Drives both the
  // per-row × and the "Clear unindexed" button.
  const removableRecents = recentWorkspaces.filter(
    (p) => !statuses[p]?.indexed && !statuses[p]?.building,
  );

  // Dense grouped list: filter (name or path) applies first, then folders
  // split into Indexed (incl. currently building) and plain Recent. Until the
  // per-path statuses load everything sits under Recent, then indexed folders
  // move up — same transient as the status badges themselves.
  const filterQuery = recentFilter.trim().toLowerCase();
  const visibleRecents = filterQuery
    ? recentWorkspaces.filter((p) => {
        const name = p.split('/').filter(Boolean).pop() || p;
        return name.toLowerCase().includes(filterQuery) || p.toLowerCase().includes(filterQuery);
      })
    : recentWorkspaces;
  const indexedRecents = visibleRecents.filter(
    (p) => statuses[p]?.indexed || statuses[p]?.building,
  );
  const unindexedRecents = visibleRecents.filter(
    (p) => !statuses[p]?.indexed && !statuses[p]?.building,
  );

  // A folder is "new" once a path is set that isn't already in Recent — those
  // already-listed folders have a ⋯ menu, so the inline steps are only for new
  // selections (typed or browsed in). Normalize trailing slashes so a typed
  // "/x/y/" still matches the canonical "/x/y" in recents.
  const normalizedPath = normalizeWsPath(path);
  const isNewFolder = normalizedPath.length > 0 && !recentWorkspaces.includes(normalizedPath);
  const newFolderSettings = settingsByPath[normalizedPath];

  // Thin wrapper: map dialog state + handlers onto the RecentWorkspaceItem
  // subcomponent. Used for both the Indexed and Recent groups.
  const renderRecentRow = (wsPath: string) => (
    <RecentWorkspaceItem
      key={wsPath}
      wsPath={wsPath}
      status={statuses[wsPath]}
      stopping={!!stopping[wsPath]}
      settings={settingsByPath[wsPath]}
      menuOpen={openMenu === wsPath}
      agentSupported={agentSupported}
      onSelect={() => handleSelectRecent(wsPath)}
      onRemoveRecent={(e) => void handleRemoveRecent(e, wsPath)}
      onToggleMenu={(e) => {
        e.stopPropagation();
        toggleMenu(wsPath);
      }}
      onStop={(e) => void handleStopIndex(e, wsPath)}
      onReindex={(e) => void handleReindex(e, wsPath)}
      onRemoveIndex={(e) => void handleRemoveIndex(e, wsPath)}
      onChange={(patch) => updateOne(wsPath, patch)}
      onEngineChange={(v) => void handleEngineChange(wsPath, v)}
    />
  );

  return (
    <div
      className="ws-connect-overlay"
      id="wsConnectOverlay"
      onPointerDown={handleOverlayPointerDown}
      onClick={handleOverlayClick}
    >
      <div className="ws-connect-dialog" ref={dialogRef} style={dialogStyle}>
        <div className="ws-connect-drag" onPointerDown={onMoveStart}>
          <h3>Connect Workspace</h3>
          <button
            type="button"
            className="ws-connect-reset"
            title="Reset size and position"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={resetDialogSize}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <polyline points="1 4 1 10 7 10" />
              <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
            </svg>
          </button>
        </div>
        <div className="ws-connect-body">
          <p>Enter a path or browse to a folder:</p>
          <div className="ws-connect-path-row">
            <input
              ref={inputRef}
              type="text"
              className="ws-connect-input"
              placeholder="/path/to/your/project"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={handleKeyDown}
            />
            <button className="btn btn-sm" type="button" onClick={handleBrowse}>
              Browse
            </button>
          </div>

          {/* Folder browser */}
          {browserOpen && (
            <WorkspaceBrowser
              current={browserCurrent}
              parent={browserParent}
              entries={sortedEntries}
              files={sortedFiles}
              fileTotal={browserFileTotal}
              loading={browserLoading}
              sortMode={sortMode}
              onSort={setSortMode}
              onUp={handleBrowseUp}
              onBrowseDir={handleBrowseDir}
            />
          )}
          {/* Recent workspaces, grouped: Indexed first, then plain recents */}
          {recentWorkspaces.length > 0 && (
            <div className="ws-recent">
              <div className="ws-recent-label">
                <span>Workspaces</span>
                {removableRecents.length > 0 && (
                  <button
                    type="button"
                    className="ws-recent-clear"
                    title="Remove every folder that isn't indexed from this list"
                    onClick={() => void handleClearUnindexed()}
                  >
                    Clear unindexed ({removableRecents.length})
                  </button>
                )}
              </div>
              {recentWorkspaces.length > 5 && (
                <input
                  type="text"
                  className="ws-recent-filter"
                  placeholder="Filter folders"
                  aria-label="Filter recent workspaces"
                  value={recentFilter}
                  onChange={(e) => setRecentFilter(e.target.value)}
                />
              )}
              <div className="ws-recent-list">
                {indexedRecents.length > 0 && <div className="ws-recent-group">Indexed</div>}
                {indexedRecents.map(renderRecentRow)}
                {unindexedRecents.length > 0 && <div className="ws-recent-group">Recent</div>}
                {unindexedRecents.map(renderRecentRow)}
                {visibleRecents.length === 0 && (
                  <div className="ws-recent-nomatch">No folders match “{recentFilter.trim()}”</div>
                )}
              </div>
            </div>
          )}

          <div className="ws-index-options">
            <label className="ws-index-toggle">
              <input
                type="checkbox"
                checked={indexOnConnect}
                onChange={(e) => setIndexOnConnect(e.target.checked)}
              />
              Index this folder for semantic search on connect
            </label>
            {indexOnConnect && isNewFolder ? (
              newFolderSettings ? (
                <div className="ws-new-settings">
                  <div className="ws-new-settings-title">Indexing options for this folder</div>
                  <IndexSettingsPanel
                    settings={newFolderSettings}
                    agentSupported={agentSupported}
                    onChange={(patch) => updateOne(normalizedPath, patch)}
                    onEngineChange={(v) => void handleEngineChange(normalizedPath, v)}
                  />
                </div>
              ) : (
                <span className="ws-index-hint">Loading indexing options…</span>
              )
            ) : (
              <span className="ws-index-hint">
                For a folder already in Recent, open its ⋯ menu to set auto-refresh, relationship
                mapping, and background refresh.
              </span>
            )}
          </div>

          <div className="ws-connect-actions">
            <button
              className="btn btn-sm ws-native-pick"
              type="button"
              title="Open the system folder picker instead"
              onClick={() => void handleNativePick()}
            >
              Native picker…
            </button>
            <button className="btn btn-sm" type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              type="button"
              onClick={handleConnect}
              disabled={connecting}
            >
              {connecting ? 'Connecting…' : 'Connect'}
            </button>
          </div>
          {error && (
            <div className="ws-connect-error" style={{ display: 'block' }}>
              {error}
            </div>
          )}
        </div>
        <div className="ws-rz ws-rz-e" onPointerDown={onResizeStart({ e: true })} />
        <div className="ws-rz ws-rz-s" onPointerDown={onResizeStart({ s: true })} />
        <div
          className="ws-rz ws-rz-se"
          onPointerDown={onResizeStart({ e: true, s: true })}
          aria-hidden="true"
        />
      </div>
    </div>
  );
};
