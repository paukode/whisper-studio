/**
 * TaskReportCard: the result of a background command this chat started,
 * which finished after the turn that started it (server/tasks/wake.py). The
 * model reads the same result as data on its next turn, which the app starts
 * on its own (src/hooks/chatStream/backgroundWake.ts).
 *
 * Drawn like an agent report (AgentReportCard.tsx, modules/agent-report.css):
 * one line with the command as its tail and the outcome as chips, collapsed
 * by default, opening to the last lines of the command's output.
 */
import React, { useState } from 'react';
import type { TaskReportPayload } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ICON } from '@/components/chat/activityIcons';

interface Props {
  report: TaskReportPayload;
}

/** How long the command ran, as "1h 43m", "2m 5s" or "40s". */
export function durationText(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || ms < 0) return '';
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** The outcome chip: finished (exit 0), failed with its exit code, or stopped
 *  (the runtime cap). */
function outcome(report: TaskReportPayload): { text: string; tone: 'done' | 'err' | 'neutral' } {
  const code = report.exit_code;
  if (report.status === 'completed') return { text: 'finished', tone: 'done' };
  if (report.status === 'stopped') return { text: 'stopped', tone: 'neutral' };
  return { text: code != null ? `failed · exit ${code}` : 'failed', tone: 'err' };
}

/** A fence longer than any run of backticks in the output, so the output
 *  renders as one code block whatever it holds. */
function fenced(text: string): string {
  const longest = Math.max(2, ...(text.match(/`+/g) ?? []).map((run) => run.length));
  const fence = '`'.repeat(longest + 1);
  return `${fence}\n${text}\n${fence}`;
}

export const TaskReportCard: React.FC<Props> = ({ report }) => {
  const [open, setOpen] = useState(false);
  const { text, tone } = outcome(report);
  const ran = durationText(report.duration_ms);
  const output = (report.output_tail ?? '').trimEnd();
  const failed = tone === 'err';
  return (
    <div className="agent-report-card" data-testid="task-report-card">
      <div className="activity-head">
        <button
          type="button"
          className={`activity-summary agent-report-line${failed ? ' failed' : ''}`}
          aria-expanded={open}
          title={report.command}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="activity-slot">{ICON.terminal}</span>
          <span className="activity-chevron" aria-hidden="true">{ICON.chevronRight}</span>
          <span className="activity-label">Background command finished</span>
          <span className="activity-tail">{report.command}</span>
          <span className="activity-meta">
            <span className={tone === 'neutral' ? 'activity-chip' : `activity-chip ${tone}`}>{text}</span>
            {ran && <span className="activity-chip">{ran}</span>}
          </span>
        </button>
      </div>
      {open && (
        <div className="agent-report-body">
          <div className="agent-report-text">
            <MarkdownRenderer content={output ? fenced(output) : '_No output._'} />
          </div>
        </div>
      )}
    </div>
  );
};
