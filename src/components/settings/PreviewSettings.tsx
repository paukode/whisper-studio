import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, post, del } from '@/api/client';

interface PreviewStatus {
  playwright_importable: boolean;
  chromium_installed: boolean;
  flag_enabled: boolean;
  installing: boolean;
  stage: string | null;
  error: string | null;
  log_tail: string[];
}

interface PreviewSessionInfo {
  id: string;
  url: string | null;
  port: number | null;
  process_alive: boolean | null;
  browser_started: boolean;
  created_at: number;
}

function relativeTime(epochSeconds: number): string {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

export const PreviewSettings: React.FC = () => {
  const queryClient = useQueryClient();

  const statusQuery = useQuery({
    queryKey: ['preview-status'],
    queryFn: () => get<PreviewStatus>('/api/preview/status'),
    refetchInterval: (query) => (query.state.data?.installing ? 1000 : 5000),
  });

  const sessionsQuery = useQuery({
    queryKey: ['preview-sessions'],
    queryFn: () => get<{ sessions: PreviewSessionInfo[] }>('/api/preview/sessions'),
    refetchInterval: 5000,
    enabled: !!statusQuery.data?.flag_enabled,
  });

  const status = statusQuery.data;
  const sessions = sessionsQuery.data?.sessions ?? [];

  const handleEnable = async () => {
    try {
      await post('/api/preview/install');
      await queryClient.invalidateQueries({ queryKey: ['preview-status'] });
    } catch (err) {
      console.warn('Failed to start preview install:', err);
    }
  };

  const handleDisable = async () => {
    try {
      await post('/api/preview/disable');
      await queryClient.invalidateQueries({ queryKey: ['preview-status'] });
    } catch (err) {
      console.warn('Failed to disable live preview:', err);
    }
  };

  const handleStop = async (name: string) => {
    try {
      await del(`/api/preview/sessions/${encodeURIComponent(name)}`);
      await queryClient.invalidateQueries({ queryKey: ['preview-sessions'] });
    } catch (err) {
      console.warn('Failed to stop preview session:', err);
    }
  };

  if (!status) {
    return <p className="settings-empty">Loading…</p>;
  }

  return (
    <div className="settings-panel">
      <div className="settings-item" style={{ alignItems: 'flex-start' }}>
        <div className="settings-item-info">
          <div className="settings-item-name">Live preview</div>
          <div className="settings-item-desc">
            Lets the assistant spin up a dev server, drive a real headless browser against it, and
            validate changes with screenshots, console output, and network activity — the same tools
            used to build this feature. Downloads Playwright and a ~150-300MB Chromium browser, and
            lets the assistant control it.
          </div>
          {status.error && (
            <div style={{ color: 'var(--accent-record)', fontSize: '13px', marginTop: '6px' }}>
              {status.error}
            </div>
          )}
          {status.installing && (
            <div style={{ marginTop: '8px' }}>
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                {status.stage ?? 'Installing…'}
              </div>
              {status.log_tail.length > 0 && (
                <pre
                  style={{
                    fontSize: '11px',
                    maxHeight: '120px',
                    overflow: 'auto',
                    background: 'var(--surface-2, rgba(127,127,127,0.08))',
                    padding: '8px',
                    borderRadius: '6px',
                  }}
                >
                  {status.log_tail.slice(-20).join('\n')}
                </pre>
              )}
            </div>
          )}
        </div>
        <div className="settings-item-actions">
          {status.flag_enabled ? (
            <>
              <span
                style={{
                  fontSize: '12px',
                  padding: '4px 10px',
                  borderRadius: 'var(--radius, 8px)',
                  background: 'var(--bg-success, rgba(80,180,120,0.15))',
                  color: 'var(--text-success, #4caf7d)',
                }}
              >
                Enabled
              </span>
              <button className="btn btn-sm" onClick={() => void handleDisable()}>
                Disable
              </button>
            </>
          ) : (
            <button className="btn btn-sm" onClick={() => void handleEnable()} disabled={status.installing}>
              {status.installing ? 'Installing…' : 'Enable live preview'}
            </button>
          )}
        </div>
      </div>

      {status.flag_enabled && (
        <div style={{ marginTop: '16px' }}>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
            Running sessions
          </div>
          {sessions.length === 0 ? (
            <p className="settings-empty">No preview sessions running.</p>
          ) : (
            <div className="settings-list">
              {sessions.map((s) => (
                <div className="settings-item" key={s.id}>
                  <div className="settings-item-info">
                    <div className="settings-item-name">{s.id}</div>
                    <div className="settings-item-desc">
                      {s.url ? s.url : s.port ? `port ${s.port}` : 'no url'} · running {relativeTime(s.created_at)}
                      {s.process_alive === false && ' · process exited'}
                    </div>
                  </div>
                  <div className="settings-item-actions">
                    <button className="btn btn-sm" onClick={() => void handleStop(s.id)}>
                      Stop
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
