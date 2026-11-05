/**
 * CompactSummaryCard: the row a /compact run leaves (server/chat/compact_route.py).
 * Everything above it now reaches the model only as this summary. Drawn like
 * a task report: one line, collapsed, opening to the summary text.
 */
import React, { useState } from 'react';
import type { CompactSummaryPayload } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ICON } from '@/components/chat/activityIcons';

interface Props {
  summary: string;
  info?: CompactSummaryPayload;
}

const HEADER = '[Context summary of earlier conversation]';

/** "48.2K", "900" chars. */
function chars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

export const CompactSummaryCard: React.FC<Props> = ({ summary, info }) => {
  const [open, setOpen] = useState(false);
  const body = summary.startsWith(HEADER) ? summary.slice(HEADER.length).trim() : summary.trim();
  return (
    <div className="agent-report-card" data-testid="compact-summary-card">
      <div className="activity-head">
        <button
          type="button"
          className="activity-summary agent-report-line"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="activity-slot">{ICON.layers}</span>
          <span className="activity-chevron" aria-hidden="true">{ICON.chevronRight}</span>
          <span className="activity-label">Conversation compacted</span>
          {info?.note && <span className="activity-tail">Kept: {info.note}</span>}
          {info && (
            <span className="activity-meta">
              <span className="activity-chip">{info.messages} messages</span>
              <span className="activity-chip">
                {chars(info.before_chars)} to {chars(info.after_chars)} chars
              </span>
            </span>
          )}
        </button>
      </div>
      {open && (
        <div className="agent-report-body">
          <div className="agent-report-text">
            <MarkdownRenderer content={body || '_Empty summary._'} />
          </div>
        </div>
      )}
    </div>
  );
};
