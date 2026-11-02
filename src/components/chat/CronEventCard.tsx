import React from 'react';
import type { CronEventPayload } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';
import { ICON } from './activityIcons';

interface CronEventCardProps {
  event: CronEventPayload;
}

/**
 * Inline rendering for a `role: 'cron_event'` ChatMessage.
 *
 * Variants by `event_type`:
 *
 *   - `cron_created` / `cron_updated`: small pill, a clock and "Cron 'X' scheduled · <label>"
 *   - `cron_fired`: full inline message; header strip + markdown body
 *   - `cron_deleted`: small pill, a bin and "Cron 'X' removed"
 *
 * The shape comes from server/cron_scheduler.py:_emit_cron_event. Both the live
 * SSE event (`type: 'cron_event'`) and the persisted chat_history row carry the
 * same payload, so the live-render and the replay-on-resume paths converge here.
 *
 * Cron event rows are UI-only: they never enter Claude's prompt (the backend's
 * visible_chat_history() filter drops them), so this card can be rich without
 * inflating token costs.
 */
export const CronEventCard: React.FC<CronEventCardProps> = ({ event }) => {
  const when = formatMessageTimestamp(event.timestamp);
  const scheduleText = event.schedule_label || '';

  if (event.event_type === 'cron_created' || event.event_type === 'cron_updated') {
    const verb = event.event_type === 'cron_updated' ? 'updated' : 'scheduled';
    return (
      <div className="cron-event cron-event-pill cron-event-created">
        <span className="cron-event-icon">{ICON.clock}</span>
        <span className="cron-event-pill-text">
          Cron <strong>{event.cron_name}</strong> {verb}
          {scheduleText && <> · {scheduleText}</>}
        </span>
        <span className="cron-event-meta">{when}</span>
      </div>
    );
  }

  if (event.event_type === 'cron_deleted') {
    return (
      <div className="cron-event cron-event-pill cron-event-deleted">
        <span className="cron-event-icon">{ICON.trash}</span>
        <span className="cron-event-pill-text">
          Cron <strong>{event.cron_name}</strong> removed
        </span>
        <span className="cron-event-meta">{when}</span>
      </div>
    );
  }

  // cron_fired: full inline message. The body is always rendered so the user
  // can read every run's output without clicking. The header strip signals
  // "this is a cron event, not a regular assistant turn."
  const failed = event.status === 'failed';
  const text = event.text ?? '(no output)';
  const stateClass = failed ? 'cron-event-fired-failed' : 'cron-event-fired-ok';
  const nextRun = event.next_run ? formatMessageTimestamp(event.next_run) : '';

  return (
    <div className={`cron-event cron-event-fired ${stateClass}`}>
      <div className="cron-event-summary">
        <span className="cron-event-icon">{failed ? ICON.alert : ICON.clock}</span>
        <span className="cron-event-title">
          Cron <strong>{event.cron_name || '(unnamed)'}</strong>{' '}
          {failed ? 'failed' : 'fired'}
        </span>
        {scheduleText && (
          <span className="cron-event-interval">· {scheduleText}</span>
        )}
        <span className="cron-event-status">
          {failed ? <>{ICON.alertCircle}failed</> : <>{ICON.check}ok</>}
          {typeof event.duration_ms === 'number' && ` · ${formatDuration(event.duration_ms)}`}
        </span>
        <span className="cron-event-meta">{when}</span>
      </div>
      <div className="cron-event-body">
        <MarkdownRenderer content={text} />
      </div>
      {nextRun && (
        <div className="cron-event-foot" style={{ fontSize: 12, opacity: 0.7, marginTop: 6 }}>
          Next run {nextRun}
        </div>
      )}
    </div>
  );
};

/** Format a run duration (ms) as "820ms" / "8.2s" / "1m 04s". */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${String(rem).padStart(2, '0')}s`;
}
