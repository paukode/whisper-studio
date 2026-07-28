import React, { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, post } from '@/api/client';
import { useSessionStore } from '@/stores/sessionStore';

interface NotificationRow {
  id: number;
  session_id: string;
  source: 'chat' | 'agent' | 'cron';
  title: string;
  message: string;
  status: string;
  created_at: string;
  read_at: string | null;
}

/**
 * Header bell over the durable notification log (`notify_user` writes it via
 * server/notifications.py; toasts stay ephemeral). The badge polls the unread
 * count so results delivered while the user was away are visible on return.
 * The dropdown lists notifications with an All/Unread filter, per-row
 * mark-read and hide, mark-all-read, and clear-all (soft-hide: rows keep
 * living in sessions.db until the 30-day GC).
 */
export const NotificationsBell: React.FC = () => {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<'all' | 'unread'>('all');
  const wrapRef = useRef<HTMLDivElement>(null);

  const unreadQuery = useQuery({
    queryKey: ['notifications-unread'],
    queryFn: () => get<{ unread: number }>('/api/notifications/unread-count'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const unread = unreadQuery.data?.unread ?? 0;

  const listQuery = useQuery({
    queryKey: ['notifications-list', filter],
    queryFn: () =>
      get<{ notifications: NotificationRow[] }>(
        `/api/notifications?limit=50${filter === 'unread' ? '&unread_only=true' : ''}`,
      ),
    enabled: open,
  });

  // Close on outside click (same pattern as the header's theme picker).
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [open]);

  // ESC closes the dropdown. Capture + preventDefault so the app-level ESC
  // kill switch (stop streams) does not also fire.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
    void queryClient.invalidateQueries({ queryKey: ['notifications-list'] });
  };

  const markAllRead = async () => {
    try {
      await post('/api/notifications/read', {});
    } catch {
      /* best-effort; the list refetch shows the truth */
    }
    refresh();
  };

  const clearAll = async () => {
    try {
      await post('/api/notifications/clear', {});
    } catch {
      /* best-effort */
    }
    refresh();
  };

  const markRead = async (id: number) => {
    try {
      await post('/api/notifications/read', { ids: [id] });
    } catch {
      /* best-effort */
    }
    refresh();
  };

  const hideOne = async (id: number) => {
    try {
      await post('/api/notifications/hide', { ids: [id] });
    } catch {
      /* best-effort */
    }
    refresh();
  };

  const openNotification = async (n: NotificationRow) => {
    if (!n.read_at) void markRead(n.id);
    if (n.session_id) {
      setOpen(false);
      void useSessionStore.getState().switchSession(n.session_id);
    }
  };

  const rows = listQuery.data?.notifications ?? [];

  return (
    <div className="notif-bell-wrap" ref={wrapRef}>
      <button
        className="btn-icon"
        id="notificationsBell"
        type="button"
        title={unread > 0 ? `${unread} unread notification${unread === 1 ? '' : 's'}` : 'Notifications'}
        aria-label="Notifications"
        aria-expanded={open}
        onClick={() => setOpen((p) => !p)}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
          <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
        </svg>
        {unread > 0 && <span className="notif-badge">{unread > 99 ? '99+' : unread}</span>}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="Notifications">
          <div className="notif-head">
            <span className="notif-title">Notifications</span>
            <div className="notif-head-actions">
              <button
                className={`notif-tab${filter === 'all' ? ' active' : ''}`}
                type="button"
                onClick={() => setFilter('all')}
              >
                All
              </button>
              <button
                className={`notif-tab${filter === 'unread' ? ' active' : ''}`}
                type="button"
                onClick={() => setFilter('unread')}
              >
                Unread{unread > 0 ? ` ${unread}` : ''}
              </button>
              <button className="bg-tasks-close" type="button" onClick={() => setOpen(false)} title="Close">
                ✕
              </button>
            </div>
          </div>
          <div className="notif-body">
            {listQuery.isLoading && <div className="notif-empty">Loading…</div>}
            {!listQuery.isLoading && rows.length === 0 && (
              <div className="notif-empty">
                {filter === 'unread' ? (
                  'Nothing unread.'
                ) : (
                  <>
                    Nothing yet. Results the assistant flags with <code>notify_user</code>
                    {' '}(chat, agents, and scheduled tasks) land here.
                  </>
                )}
              </div>
            )}
            {rows.map((n) => (
              <div key={n.id} className={`notif-row${n.read_at ? '' : ' notif-row-unread'}`}>
                <button
                  type="button"
                  className="notif-row-body"
                  onClick={() => void openNotification(n)}
                  title={n.session_id ? 'Open the session this came from' : undefined}
                >
                  <span className={`notif-source notif-source-${n.source}`}>{n.source}</span>
                  <span className="notif-row-main">
                    {n.title && <span className="notif-row-title">{n.title}</span>}
                    <span className="notif-row-msg">{n.message}</span>
                  </span>
                  <span className="notif-time">{relativeTime(n.created_at)}</span>
                </button>
                <span className="notif-row-actions">
                  {!n.read_at && (
                    <button
                      type="button"
                      className="notif-action"
                      title="Mark as read"
                      onClick={() => void markRead(n.id)}
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M20 6 9 17l-5-5"/>
                      </svg>
                    </button>
                  )}
                  <button
                    type="button"
                    className="notif-action"
                    title="Hide this notification"
                    onClick={() => void hideOne(n.id)}
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 6 6 18M6 6l12 12"/>
                    </svg>
                  </button>
                </span>
              </div>
            ))}
          </div>
          {rows.length > 0 && (
            <div className="notif-foot">
              {unread > 0 ? (
                <button className="btn btn-sm" type="button" onClick={() => void markAllRead()}>
                  Mark all read
                </button>
              ) : (
                <span />
              )}
              <button className="btn btn-sm" type="button" onClick={() => void clearAll()}>
                Clear all
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

/** Compact relative timestamp; falls back to the date beyond a week. */
function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 604800) return `${Math.floor(s / 86400)}d`;
  return new Date(t).toLocaleDateString();
}
