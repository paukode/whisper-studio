import React from 'react';
import { useTaskStore, taskProgress } from '@/stores/taskStore';
import { useSessionStore } from '@/stores/sessionStore';

function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') {
    return (
      <svg className="tasks-drawer-icon done" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    );
  }
  if (status === 'in_progress') {
    return (
      <svg className="tasks-drawer-icon running" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
        <path d="M21 12a9 9 0 1 1-6.2-8.56" />
      </svg>
    );
  }
  return (
    <svg className="tasks-drawer-icon pending" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
    </svg>
  );
}

/**
 * TasksPanel — the tasks list body for a dock panel. Reads the active session's
 * task list from taskStore (fed by the `todo_update` SSE feed). Extracted from
 * TasksDrawer so the same content renders inside the RightDock instead of a
 * separate slide-over.
 */
export const TasksPanel: React.FC = () => {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const tasksBySession = useTaskStore((s) => s.tasksBySession);
  const tasks = (currentSessionId ? tasksBySession[currentSessionId] : undefined) ?? [];
  const { done, total } = taskProgress(tasks);
  const pct = total ? Math.round((done / total) * 100) : 0;

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, overflow: 'auto', padding: '8px 0' }}>
      <div
        className="tasks-drawer-bar"
        role="progressbar"
        aria-label="Task progress"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={`${done} of ${total} done`}
        style={{ margin: '0 12px 8px' }}
      >
        <div className="tasks-drawer-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="sr-only" aria-live="polite">{`${done} of ${total} tasks done`}</div>
      {total === 0 ? (
        <div className="tasks-drawer-empty" style={{ padding: '12px' }}>No tasks for this conversation yet.</div>
      ) : (
        <ul className="tasks-drawer-list">
          {tasks.map((t) => (
            <li key={t.id} className={`tasks-drawer-item status-${t.status}`} aria-label={`${t.subject} — ${t.status.replace('_', ' ')}`}>
              <StatusIcon status={t.status} />
              <span className="tasks-drawer-subject">{t.subject}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export default TasksPanel;
