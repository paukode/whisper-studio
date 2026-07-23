import React, { useEffect } from 'react';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import type { BackgroundTaskInfo } from '@/types/backgroundTasks';

/**
 * Global "what's running" panel: every background task (shell command,
 * detached agent, workflow run) across ALL sessions, running first. Toggled
 * from the Header pill; ESC closes it (preventDefault so the chat-stream
 * kill switch doesn't also fire).
 */
export const BackgroundTasksPanel: React.FC = () => {
  const panelOpen = useBackgroundTaskStore((s) => s.panelOpen);
  const setPanelOpen = useBackgroundTaskStore((s) => s.setPanelOpen);
  const tasks = useBackgroundTaskStore((s) => s.tasks);
  const hydrate = useBackgroundTaskStore((s) => s.hydrate);
  const stopTask = useBackgroundTaskStore((s) => s.stopTask);

  useEffect(() => {
    if (panelOpen) void hydrate();
  }, [panelOpen, hydrate]);

  useEffect(() => {
    if (!panelOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        setPanelOpen(false);
      }
    };
    // Capture phase so this wins over the app-level ESC kill switch.
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [panelOpen, setPanelOpen]);

  if (!panelOpen) return null;

  const rows = Object.values(tasks).sort(byRunningThenNewest);

  return (
    <div className="bg-tasks-panel" role="dialog" aria-label="Background tasks">
      <div className="bg-tasks-head">
        <span className="bg-tasks-title">Background tasks</span>
        <button className="bg-tasks-close" onClick={() => setPanelOpen(false)} title="Close">
          ✕
        </button>
      </div>
      <div className="bg-tasks-body">
        {rows.length === 0 && <div className="bg-tasks-empty">Nothing running or recent.</div>}
        {rows.map((t) => (
          <div key={t.task_id} className={`bg-tasks-row bg-tasks-${t.status}`}>
            <div className="bg-tasks-row-main">
              <span className="bg-tasks-kind">{t.kind}</span>
              <span className="bg-tasks-row-title" title={t.command ?? t.title}>
                {t.title}
              </span>
            </div>
            <div className="bg-tasks-row-side">
              <span className="bg-tasks-status">{statusLabel(t)}</span>
              {t.status === 'running' && (
                <button
                  className="task-event-stop"
                  onClick={() => void stopTask(t.task_id)}
                  title="Stop this task"
                >
                  Stop
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

function byRunningThenNewest(a: BackgroundTaskInfo, b: BackgroundTaskInfo): number {
  const ar = a.status === 'running' ? 0 : 1;
  const br = b.status === 'running' ? 0 : 1;
  if (ar !== br) return ar - br;
  return (b.created_at || '').localeCompare(a.created_at || '');
}

function statusLabel(t: BackgroundTaskInfo): string {
  if (t.status === 'running') return 'running';
  if (t.status === 'completed') return t.exit_code === 0 || t.exit_code == null ? 'done' : `exit ${t.exit_code}`;
  if (t.status === 'failed') return t.exit_code != null ? `failed (exit ${t.exit_code})` : 'failed';
  return t.status;
}
