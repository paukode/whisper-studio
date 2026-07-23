import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSessionStore } from '@/stores/sessionStore';
import { useSessionActivity } from '@/stores/sessionRuntimes';
import { useCronUnreadStore, useCronUnreadFor } from '@/stores/cronUnreadStore';
import { useUIStore, dialogConfirm } from '@/stores/uiStore';
import { ContextMenu, type MenuItem } from '@/components/common/ContextMenu';
import * as sessionsApi from '@/api/sessions';
import { fetchRecentRuns } from '@/api/cron';
import { formatMessageTimestamp, formatSegmentTimestamp } from '@/utils/formatTimestamp';
import { stripMarkdownTitle } from '@/utils/stripMarkdownTitle';
import { useLocalStorage } from '@/hooks/useLocalStorage';

type ExportFormat = 'conversation-txt' | 'transcript-txt' | 'combined-md';

export interface SidebarProps {
  collapsed: boolean;
}

/**
 * Format session timestamp: shows time for today, date+time for older sessions.
 * Examples: "2:30 PM", "Yesterday 4:15 PM", "Apr 21 9:00 AM"
 */
function formatSessionTime(dateStr: string): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const timeStr = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

  // Today: show just time
  if (diff < 86400000 && d.getDate() === now.getDate()) {
    return timeStr;
  }
  // Yesterday
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.getDate() === yesterday.getDate() && d.getMonth() === yesterday.getMonth() && d.getFullYear() === yesterday.getFullYear()) {
    return `Yesterday ${timeStr}`;
  }
  // This year: "Apr 21 9:00 AM"
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr;
  }
  // Older: "Apr 21, 2024"
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export type SessionBucket = 'Today' | 'Yesterday' | 'Last week' | 'Last month' | 'Older';

/** Date bucket for grouping the session list. Rolling windows measured in
 *  local calendar days (not raw milliseconds) so a session from six days
 *  ago stays in "Last week" for the whole day, regardless of time of day.
 *  ``now`` is injectable for tests. */
export function bucketFor(dateStr: string, now: Date = new Date()): SessionBucket {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return 'Older';
  const sameDay = (a: Date, b: Date) =>
    a.getDate() === b.getDate() && a.getMonth() === b.getMonth() && a.getFullYear() === b.getFullYear();
  if (sameDay(d, now)) return 'Today';
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameDay(d, yesterday)) return 'Yesterday';
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.floor(
    (startOfDay(now).getTime() - startOfDay(d).getTime()) / 86400000,
  );
  if (days <= 7) return 'Last week';
  if (days <= 30) return 'Last month';
  return 'Older';
}

type SessionGroup = SessionBucket | 'Pinned' | 'Archived';

/** Flags outrank dates: archived sessions live in their own trailing
 *  group regardless of age, pinned ones float above everything. */
export function bucketForSession(
  s: { date: string; pinned?: boolean; archived?: boolean },
  now?: Date,
): SessionGroup {
  if (s.archived) return 'Archived';
  if (s.pinned) return 'Pinned';
  return bucketFor(s.date, now);
}

const BUCKET_ORDER: SessionGroup[] = [
  'Pinned', 'Today', 'Yesterday', 'Last week', 'Last month', 'Older', 'Archived',
];

/** Live-activity badge for one session row: red pulse while it owns the
 *  recording, spinner while its chat streams in the background, amber
 *  pause mark while it waits on an approval. */
const SessionActivityBadge: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const activity = useSessionActivity(sessionId);
  if (!activity) return null;
  return (
    <span className="session-activity" aria-label={`Session is ${activity}`}>
      {activity === 'recording' && <span className="pulse-dot" title="Recording" />}
      {activity === 'streaming' && <span className="session-activity-spinner" title="Responding…" />}
      {activity === 'approval' && <span className="session-activity-approval" title="Waiting for your approval">⏸</span>}
    </span>
  );
};

/** Unread-scheduled-runs badge: "⏱ N" on a session that received cron
 *  results while it wasn't focused; red when any of those runs failed.
 *  Clears when the session is opened (sessionStore.switchSession → markSeen). */
const SessionCronBadge: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const { count, hasFailure } = useCronUnreadFor(sessionId);
  if (count <= 0) return null;
  const label = `${count} new scheduled ${count === 1 ? 'result' : 'results'}${
    hasFailure ? ' — a run failed' : ''
  }`;
  return (
    <span
      className="session-cron-badge"
      title={label}
      aria-label={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        fontSize: 10,
        fontFamily: 'var(--font-mono, ui-monospace, monospace)',
        fontWeight: 700,
        padding: '1px 7px',
        borderRadius: 20,
        lineHeight: 1.7,
        color: '#fff',
        background: hasFailure ? 'var(--accent-record, #d1495b)' : 'var(--accent, #0d8a93)',
      }}
    >
      ⏱ {count}
    </span>
  );
};

/**
 * Sidebar matching the vanilla templates/index.html structure exactly.
 *
 * Structure:
 *   aside.sidebar.collapsed
 *     div.sidebar-header
 *       div.sidebar-brand (svg + h2)
 *     div.session-list#sessionList
 *       div.session-item (for each session)
 *     div.sidebar-resize-handle#sidebarResizeHandle
 */
export const Sidebar: React.FC<SidebarProps> = ({ collapsed }) => {
  const sessions = useSessionStore((s) => s.sessions);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const switchSession = useSessionStore((s) => s.switchSession);
  const deleteSession = useSessionStore((s) => s.deleteSession);
  const bulkDeleteSessions = useSessionStore((s) => s.bulkDeleteSessions);
  const setSessionFlags = useSessionStore((s) => s.setSessionFlags);
  const branchSession = useSessionStore((s) => s.branchSession);
  const addToast = useUIStore((s) => s.addToast);
  const groupsCollapsed = useUIStore((s) => s.sessionGroupsCollapsed);
  const setSessionGroupCollapsed = useUIStore((s) => s.setSessionGroupCollapsed);
  const [resizing, setResizing] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  // Remember the user's chosen width across collapse/expand cycles.
  // Without this, expanding after a collapse would reset to the CSS
  // default (272px) instead of returning to whatever the user dragged
  // to. ``null`` means the user hasn't resized yet — use CSS default.
  const resizedWidthRef = useRef<number | null>(null);

  // Inline rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // Session search filter. Titles always match locally; the optional
  // content scope asks the server which sessions also match by message
  // or transcript text (full content never lives client-side).
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [searchContent, setSearchContent] = useLocalStorage<boolean>('whisper_search_content', false);

  // Debounce the raw input by 300ms so the content toggle doesn't turn
  // every keystroke into an HTTP request (WorkspacePanel pattern).
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  // Authoritative unread-cron feed: poll recent runs and merge into the badge
  // store. Works for every session (not just the ≤3 with a live runtime); the
  // SSE live-bump makes badges appear instantly, this keeps them correct.
  const setCronRuns = useCronUnreadStore((s) => s.setRuns);
  const recentRuns = useQuery({
    queryKey: ['cron-recent-runs'],
    queryFn: () => fetchRecentRuns(200),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  useEffect(() => {
    if (recentRuns.data?.runs) setCronRuns(recentRuns.data.runs);
  }, [recentRuns.data, setCronRuns]);

  const contentSearch = useQuery({
    queryKey: ['session-content-search', debouncedQuery],
    queryFn: () => sessionsApi.searchSessions(debouncedQuery),
    enabled: searchContent && debouncedQuery.length > 0,
    staleTime: 15_000,
    // Keep the previous query's results while the next one settles so
    // content-matched rows don't blink out on every keystroke.
    placeholderData: (prev) => prev,
  });
  // id -> snippet for the current results; empty when the toggle is off.
  const contentMatches = useMemo(
    () => new Map((searchContent ? contentSearch.data?.results ?? [] : []).map((r) => [r.id, r.snippet])),
    [searchContent, contentSearch.data],
  );

  // Multi-select delete mode. Selection is transient component state:
  // leaving select mode (or deleting) always clears it.
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Kebab menu: which row is open + where to render the popup.
  const [menuFor, setMenuFor] = useState<{ id: string; x: number; y: number } | null>(null);

  const handleSwitchSession = useCallback(
    (id: string) => {
      void switchSession(id);
    },
    [switchSession],
  );

  const startRename = useCallback((id: string, title: string) => {
    setRenamingId(id);
    setRenameValue(title);
  }, []);

  const handleRenameCommit = useCallback(() => {
    if (renamingId && renameValue.trim()) {
      const newTitle = renameValue.trim();
      // Mirror the /rename slash-command path: local update + server PATCH.
      useSessionStore.getState().updateSessionTitle(renamingId, newTitle, true);
      fetch(`/api/sessions/${renamingId}/title`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle, customTitle: true }),
      }).catch(() => {});
      addToast({ type: 'success', message: `Session renamed to "${newTitle}"`, duration: 2000 });
    }
    setRenamingId(null);
  }, [renamingId, renameValue, addToast]);

  // Export this session in one of three formats. Each format mirrors an
  // existing entry point so users get identical output regardless of where
  // they trigger the export from:
  //   conversation-txt → matches /export slash command + ChatInput export
  //   transcript-txt   → matches TranscriptionPanel "Export" button
  //   combined-md      → matches ChatPanel header "Export" button (.md)
  const handleExport = useCallback(
    async (id: string, title: string, format: ExportFormat) => {
      try {
        const session = await sessionsApi.getSession(id);
        const messages = session.chatHistory ?? [];
        const segments = session.segments ?? [];
        const speakerNames = session.speakerNames ?? {};
        const slug =
          (title || 'session').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') ||
          'session';

        let blob: Blob;
        let filename: string;

        if (format === 'conversation-txt') {
          if (messages.length === 0) {
            addToast({ type: 'info', message: 'No chat messages to export.', duration: 2500 });
            return;
          }
          const text = messages.map((m) => `[${m.role}] ${m.content}`).join('\n\n---\n\n');
          blob = new Blob([text], { type: 'text/plain' });
          filename = `chat-export-${slug}-${Date.now()}.txt`;
        } else if (format === 'transcript-txt') {
          if (segments.length === 0) {
            addToast({ type: 'info', message: 'No transcript to export.', duration: 2500 });
            return;
          }
          const text = segments
            .map((s) => `[${speakerNames[s.speaker] ?? s.speaker}] ${s.text}`)
            .join('\n');
          blob = new Blob([text], { type: 'text/plain' });
          filename = `transcript-${slug}-${Date.now()}.txt`;
        } else {
          // combined-md — chat + transcript in one Markdown file. Bail
          // if both are empty; otherwise include whichever sections exist.
          if (messages.length === 0 && segments.length === 0) {
            addToast({ type: 'info', message: 'Nothing to export.', duration: 2500 });
            return;
          }
          let md = `# Conversation Export\n\n`;
          md += `**Session:** ${title}\n`;
          md += `**Exported:** ${new Date().toISOString()}\n\n---\n\n`;
          for (const msg of messages) {
            const time = formatMessageTimestamp(msg.timestamp);
            if (msg.role === 'user') {
              md += `## You (${time})\n\n${msg.content}\n\n`;
            } else {
              md += `## Assistant (${time})\n\n${msg.content ?? ''}\n\n`;
            }
            md += `---\n\n`;
          }
          if (segments.length > 0) {
            md += `\n# Transcription\n\n`;
            for (const seg of segments) {
              const speaker = speakerNames[seg.speaker] ?? seg.speaker;
              const time = formatSegmentTimestamp(seg.timestamp);
              md += `**[${time}] ${speaker}:** ${seg.text}\n\n`;
            }
          }
          blob = new Blob([md], { type: 'text/markdown' });
          filename = `conversation-${slug}-${Date.now()}.md`;
        }

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
        addToast({ type: 'success', message: 'Exported.', duration: 2000 });
      } catch {
        addToast({ type: 'error', message: 'Failed to export session.', duration: 3000 });
      }
    },
    [addToast],
  );

  const handleDelete = useCallback(
    (id: string, title: string) => {
      void (async () => {
        const ok = await dialogConfirm({
          title: 'Delete session?',
          message: `Delete session "${title}"? This cannot be undone.`,
          danger: true,
          confirmText: 'Delete',
        });
        if (ok === true) void deleteSession(id);
      })();
    },
    [deleteSession],
  );

  // Open the kebab menu anchored just below the trigger button.
  const openMenu = useCallback((e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setMenuFor({ id, x: rect.right - 4, y: rect.bottom + 4 });
  }, []);

  // Open the session's workspace folder in an external app. The server
  // resolves the path from the DB; failures (app not installed, workspace
  // folder moved) come back as a message we surface verbatim.
  const handleOpenWorkspace = useCallback(
    (id: string, app: sessionsApi.WorkspaceApp) => {
      void sessionsApi.openSessionWorkspace(id, app).catch((err: unknown) => {
        const message = err instanceof Error && err.message ? err.message : 'Could not open workspace';
        addToast({ type: 'error', message, duration: 3000 });
      });
    },
    [addToast],
  );

  // ── Multi-select delete ──
  const exitSelectMode = useCallback(() => {
    setSelectMode(false);
    setSelected(new Set());
  }, []);

  const toggleSelected = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleBulkDelete = useCallback(() => {
    const ids = [...selected];
    if (ids.length === 0) return;
    void (async () => {
      const ok = await dialogConfirm({
        title: ids.length === 1 ? 'Delete session?' : `Delete ${ids.length} sessions?`,
        message:
          ids.length === 1
            ? 'Delete the selected session? This cannot be undone.'
            : `Delete ${ids.length} selected sessions? This cannot be undone.`,
        danger: true,
        confirmText: 'Delete',
      });
      if (ok !== true) return;
      await bulkDeleteSessions(ids);
      exitSelectMode();
      addToast({
        type: 'success',
        message: ids.length === 1 ? 'Session deleted' : `${ids.length} sessions deleted`,
        duration: 2500,
      });
    })();
  }, [selected, bulkDeleteSessions, exitSelectMode, addToast]);

  // Sidebar resize handle. Writes the chosen width as an inline style
  // for immediate effect and also stashes it in a ref so we can
  // restore it after a collapse/expand cycle.
  useEffect(() => {
    if (!resizing) return;
    const handleMouseMove = (e: MouseEvent) => {
      if (sidebarRef.current) {
        const width = Math.max(200, Math.min(500, e.clientX));
        sidebarRef.current.style.width = `${width}px`;
        resizedWidthRef.current = width;
      }
    };
    const handleMouseUp = () => setResizing(false);
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [resizing]);

  // Reconcile inline width with the collapsed state. Critical because
  // an inline ``style.width`` from a previous drag beats the
  // ``.sidebar.collapsed { width: 0 }`` CSS selector — without this
  // effect the sidebar's content fades out but the container stays at
  // its dragged width. useLayoutEffect runs before the browser paints
  // so the CSS transition animates in a single step instead of two.
  useLayoutEffect(() => {
    const el = sidebarRef.current;
    if (!el) return;
    if (collapsed) {
      // Drop the inline override so ``.sidebar.collapsed`` can win.
      el.style.width = '';
    } else if (resizedWidthRef.current !== null) {
      // Restore the user's chosen width on expand.
      el.style.width = `${resizedWidthRef.current}px`;
    }
    // else: no remembered width — leave the inline style empty so the
    // CSS default (272px) applies.
  }, [collapsed]);

  // Search filter, shared by the list rendering and Select-all (which
  // operates on what the user can currently see, not the whole store).
  // Content matches are merged by id so `filtered` stays a subset of the
  // store's sessions and bucket grouping / bulk actions work unchanged.
  const q = query.trim().toLowerCase();
  const filtered = q
    ? sessions.filter((s) => s.title.toLowerCase().includes(q) || contentMatches.has(s.id))
    : sessions;
  const allSelected = filtered.length > 0 && filtered.every((s) => selected.has(s.id));
  const handleSelectAll = useCallback(() => {
    setSelected(allSelected ? new Set() : new Set(filtered.map((s) => s.id)));
  }, [allSelected, filtered]);

  const menuSession = menuFor ? sessions.find((s) => s.id === menuFor.id) : null;
  // Export always offers all three formats, with the combined file FIRST so
  // "conversation + transcript together" is the default one-click choice —
  // mirroring how the DB persists both together. The submenu shows even
  // when there's no transcript yet: combined-md falls back to just the
  // conversation, and transcript-only no-ops with a toast if empty.
  const exportItem: MenuItem = menuSession
    ? {
        label: 'Export',
        children: [
          {
            label: 'Conversation + transcript (.md)',
            onClick: () => {
              void handleExport(menuSession.id, menuSession.title, 'combined-md');
            },
          },
          {
            label: 'Conversation (.txt)',
            onClick: () => {
              void handleExport(menuSession.id, menuSession.title, 'conversation-txt');
            },
          },
          {
            label: 'Transcript (.txt)',
            onClick: () => {
              void handleExport(menuSession.id, menuSession.title, 'transcript-txt');
            },
          },
        ],
      }
    : { label: '' };

  // The menu derives each item's keycap from its label's first letter
  // (letterShortcuts): Pin -> P, Rename -> R, … On a first-letter collision
  // the earlier item wins, so keep the most-used actions first.
  const menuItems: MenuItem[] = menuSession
    ? [
        {
          label: menuSession.pinned ? 'Unpin' : 'Pin',
          onClick: () => setSessionFlags(menuSession.id, { pinned: !menuSession.pinned }),
        },
        {
          label: 'Rename',
          onClick: () => startRename(menuSession.id, menuSession.title),
        },
        {
          label: 'Branch',
          onClick: () => void branchSession(menuSession.id),
        },
        exportItem,
        ...(menuSession.workspacePath
          ? [{
              label: 'Open workspace in',
              children: [
                { label: 'VS Code', onClick: () => handleOpenWorkspace(menuSession.id, 'vscode') },
                { label: 'Kiro', onClick: () => handleOpenWorkspace(menuSession.id, 'kiro') },
                { label: 'Finder', onClick: () => handleOpenWorkspace(menuSession.id, 'finder') },
              ],
            } satisfies MenuItem]
          : []),
        { separator: true, label: '' },
        {
          label: menuSession.archived ? 'Unarchive' : 'Archive',
          onClick: () => setSessionFlags(menuSession.id, { archived: !menuSession.archived }),
        },
        {
          label: 'Delete',
          danger: true,
          onClick: () => handleDelete(menuSession.id, menuSession.title),
        },
      ]
    : [];

  return (
    <aside
      ref={sidebarRef}
      className={`sidebar${collapsed ? ' collapsed' : ''}${resizing ? ' dragging' : ''}`}
      id="sidebar"
    >
      <div className="sidebar-header">
        <div className="sidebar-brand">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
          </svg>
          <h2>Conversations</h2>
        </div>
      </div>

      {sessions.length > 0 && (
        <div className="session-toolbar">
          <div className="session-search">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" />
            </svg>
            <input
              type="text"
              className="session-search-input"
              placeholder="Search conversations…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {query && (
              <button type="button" className="session-search-clear" onClick={() => setQuery('')} aria-label="Clear search">×</button>
            )}
          </div>
          <button
            type="button"
            className={`session-select-toggle${selectMode ? ' active' : ''}`}
            title="Select multiple sessions"
            aria-pressed={selectMode}
            onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
          >
            {selectMode ? 'Done' : 'Select'}
          </button>
        </div>
      )}
      {sessions.length > 0 && query.trim() !== '' && (
        <label className="session-search-scope" title="Also match message and transcript text, not just titles">
          <input
            type="checkbox"
            checked={searchContent}
            onChange={(e) => setSearchContent(e.target.checked)}
          />
          <span>Search message content</span>
          {searchContent && contentSearch.isFetching && <span className="session-activity-spinner" />}
          {searchContent && !contentSearch.isFetching && contentSearch.isError && (
            <span className="session-search-scope-note error">search failed</span>
          )}
          {searchContent && !contentSearch.isError && contentSearch.data?.truncated && (
            <span className="session-search-scope-note">not all matches shown</span>
          )}
        </label>
      )}

      <div className="session-list" id="sessionList">
        {sessions.length === 0 && (
          <div className="session-list-empty">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
            </svg>
            <p>No conversations yet</p>
            <span>Hit Record or just start typing below</span>
          </div>
        )}
        {(() => {
          if (sessions.length > 0 && filtered.length === 0) {
            // With the content scope on: don't declare "no match" while the
            // debounce or the request is still settling, and never claim it
            // when the search itself failed (a silent false negative).
            const contentPending =
              searchContent && q !== '' &&
              (contentSearch.isFetching || debouncedQuery !== query.trim());
            const contentFailed = searchContent && q !== '' && contentSearch.isError;
            return (
              <div className="session-list-empty">
                <span>
                  {contentPending
                    ? 'Searching message content…'
                    : contentFailed
                      ? 'Content search failed. Only titles were searched.'
                      : `No conversations match “${query}”.`}
                </span>
              </div>
            );
          }

          const renderRow = (session: typeof sessions[number]) => (
            <div
              key={session.id}
              className={`session-item${session.id === currentSessionId ? ' active' : ''}${
                selectMode && selected.has(session.id) ? ' selected' : ''
              }`}
              onClick={() =>
                selectMode ? toggleSelected(session.id) : handleSwitchSession(session.id)
              }
            >
              {selectMode && (
                <input
                  type="checkbox"
                  className="session-select-checkbox"
                  checked={selected.has(session.id)}
                  onChange={() => toggleSelected(session.id)}
                  onClick={(e) => e.stopPropagation()}
                  aria-label={`Select session ${session.title}`}
                />
              )}
              {renamingId === session.id ? (
                <input
                  className="session-rename-input"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={handleRenameCommit}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleRenameCommit();
                    if (e.key === 'Escape') { e.preventDefault(); setRenamingId(null); }
                  }}
                  autoFocus
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <div
                  className="session-item-title"
                  onDoubleClick={() => startRename(session.id, session.title)}
                >
                  {/* Display-only markdown strip: auto-derived titles are the
                    * first message text, so leaked "#" / "**" tokens show up
                    * literally. Rename still edits the raw stored title. */}
                  {stripMarkdownTitle(session.title)}
                </div>
              )}
              <div
                className="session-item-meta"
                style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}
              >
                {/* Pin marker so pinned rows stay recognizable when search
                  * force-expands all groups and mixes them together. */}
                <span>
                  {session.pinned && !session.archived && 'Pinned · '}
                  {formatSessionTime(session.date)}
                  {session.chatCount > 0 && ` · ${session.chatCount} messages`}
                </span>
                <SessionCronBadge sessionId={session.id} />
              </div>
              {q !== '' && contentMatches.has(session.id) && (
                <div className="session-item-snippet">{contentMatches.get(session.id)}</div>
              )}
              <SessionActivityBadge sessionId={session.id} />
              {!selectMode && (
                <button
                  className={`session-menu-btn${menuFor?.id === session.id ? ' active' : ''}`}
                  onClick={(e) => openMenu(e, session.id)}
                  title="Session options"
                  type="button"
                  aria-label="Session options"
                  aria-haspopup="menu"
                  aria-expanded={menuFor?.id === session.id}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <circle cx="12" cy="5" r="2" />
                    <circle cx="12" cy="12" r="2" />
                    <circle cx="12" cy="19" r="2" />
                  </svg>
                </button>
              )}
            </div>
          );

          // Group into Pinned / date windows / Archived, preserving the
          // store's newest-first order within each bucket. A search query
          // force-expands every group so matches can never hide behind a
          // collapsed header. Archived defaults collapsed (out of the way
          // until asked for); an explicit toggle persists either way.
          return BUCKET_ORDER.map((bucket) => {
            const rows = filtered.filter((s) => bucketForSession(s) === bucket);
            if (rows.length === 0) return null;
            const isCollapsed = !q && (groupsCollapsed[bucket] ?? bucket === 'Archived');
            return (
              <div key={bucket} className="session-group">
                <button
                  type="button"
                  className={`session-group-header${isCollapsed ? ' collapsed' : ''}`}
                  onClick={() => setSessionGroupCollapsed(bucket, !isCollapsed)}
                  aria-expanded={!isCollapsed}
                >
                  <svg
                    className="session-group-chevron"
                    width="10" height="10" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
                    strokeLinejoin="round" aria-hidden="true"
                  >
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                  <span>{bucket}</span>
                  <span className="session-group-count">{rows.length}</span>
                </button>
                {!isCollapsed && rows.map(renderRow)}
              </div>
            );
          });
        })()}
      </div>

      {selectMode && (
        <div className="session-select-bar">
          <span className="session-select-count">{selected.size} selected</span>
          <button type="button" className="btn btn-sm" onClick={handleSelectAll}>
            {allSelected ? 'Clear' : 'Select all'}
          </button>
          <button
            type="button"
            className="btn btn-sm session-select-delete"
            disabled={selected.size === 0}
            onClick={handleBulkDelete}
          >
            Delete
          </button>
        </div>
      )}

      {menuFor && menuSession && (
        <ContextMenu
          items={menuItems}
          position={{ x: menuFor.x, y: menuFor.y }}
          onClose={() => setMenuFor(null)}
          className="ws-context-menu--compact"
          letterShortcuts
        />
      )}

      <div
        className="sidebar-resize-handle"
        id="sidebarResizeHandle"
        onMouseDown={() => setResizing(true)}
      ></div>
    </aside>
  );
};
