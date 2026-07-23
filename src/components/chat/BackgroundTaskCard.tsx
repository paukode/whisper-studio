import React from 'react';
import type { TaskEventPayload } from '@/types/chat';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';

interface BackgroundTaskCardProps {
  event: TaskEventPayload;
}

const KIND_ICON: Record<string, string> = {
  shell: '⌘',
  agent: '🤖',
  workflow: '⚙',
};

/**
 * Inline rendering for a `role: 'task_event'` ChatMessage (background shell
 * command, detached agent, or workflow run). Mirrors the CronEventCard
 * contract: the live SSE frame and the persisted row carry the same payload,
 * so live-render and replay-on-resume converge here. UI-only — never enters
 * the model's prompt.
 */
export const BackgroundTaskCard: React.FC<BackgroundTaskCardProps> = ({ event }) => {
  const stopTask = useBackgroundTaskStore((s) => s.stopTask);
  const liveStatus = useBackgroundTaskStore((s) => s.tasks[event.task_id]?.status);
  const when = formatMessageTimestamp(event.timestamp);
  const icon = KIND_ICON[event.kind] ?? '⌘';

  if (event.event_type === 'task_started') {
    // Only the store's LIVE status makes a pill claim "running": a historical
    // start pill rendered on session resume (store has no row) must not show
    // a Stop button for a task that finished long ago.
    const stillRunning = liveStatus === 'running';
    return (
      <div className="task-event task-event-pill">
        <span className="task-event-icon">{icon}</span>
        <span className="task-event-pill-text">
          {event.kind === 'shell' ? 'Background command' : `Background ${event.kind}`}{' '}
          <strong className="task-event-title">{event.title}</strong>
          {stillRunning ? ' running…' : ' started'}
        </span>
        {stillRunning && (
          <button
            className="task-event-stop"
            onClick={() => void stopTask(event.task_id)}
            title="Stop this background task"
          >
            Stop
          </button>
        )}
        <span className="task-event-meta">{when}</span>
      </div>
    );
  }

  const failed = event.event_type === 'task_failed';
  const stopped = event.event_type === 'task_stopped';
  const stateIcon = failed ? '⚠' : stopped ? '■' : '✓';
  const stateWord = failed ? 'failed' : stopped ? 'stopped' : 'finished';
  const stateClass = failed
    ? 'task-event-done-failed'
    : stopped
      ? 'task-event-done-stopped'
      : 'task-event-done-ok';

  return (
    <div className={`task-event task-event-done ${stateClass}`}>
      <div className="task-event-summary">
        <span className="task-event-icon">{icon}</span>
        <span className="task-event-title-row">
          {event.kind === 'shell' ? 'Background command' : `Background ${event.kind}`}{' '}
          <strong className="task-event-title">{event.title}</strong> {stateWord}
        </span>
        <span className="task-event-status">
          {stateIcon} {stateWord}
          {typeof event.exit_code === 'number' && ` · exit ${event.exit_code}`}
          {typeof event.duration_ms === 'number' && ` · ${formatDuration(event.duration_ms)}`}
        </span>
        <span className="task-event-meta">{when}</span>
      </div>
      {event.result_tail && (
        <pre className="task-event-tail">{event.result_tail}</pre>
      )}
    </div>
  );
};

/** "820ms" / "8.2s" / "1m 04s" — same scale as the cron card. */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${String(rem).padStart(2, '0')}s`;
}
