/**
 * AgentReportCard — reports from agents that finished with no live turn to
 * carry them: a team whose launching turn was cancelled, a background agent,
 * or an agent resumed after it had stopped. One collapsible row per agent,
 * the report rendered as markdown, the stop reason named. The backend shows
 * the same content to the model as a user turn on the next message.
 */
import React from 'react';
import type { AgentReportPayload, AgentReportRow } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';

interface Props {
  report: AgentReportPayload;
}

const REASON_LABEL: Record<string, string> = {
  completed: 'finished',
  turn_limit: 'stopped at the turn limit, report written',
  deadline: 'stopped at the time limit, report written',
  cost_cap: 'stopped at the cost cap, report written',
  cancelled: 'cancelled, report salvaged by the runtime',
  error: 'failed',
  stopped: 'stopped',
  failed: 'failed',
};

function rowLabel(a: AgentReportRow): string {
  const reason = REASON_LABEL[a.stop_reason ?? a.status] ?? (a.stop_reason ?? a.status);
  const turns = a.turns_used ? ` · ${a.turns_used} turn${a.turns_used === 1 ? '' : 's'}` : '';
  return reason + turns;
}

export const AgentReportCard: React.FC<Props> = ({ report }) => {
  const agents = report.agents ?? [];
  return (
    <div className="agent-report-card" data-testid="agent-report-card">
      <div className="agent-report-head">
        <span className="agent-report-title">Agent reports: {report.team_name}</span>
        {report.reason && <span className="agent-report-reason">{report.reason}</span>}
      </div>
      {agents.map((a, i) => (
        <details key={a.agent_id ?? `${a.name ?? 'agent'}-${i}`} className="agent-step done" open={agents.length === 1}>
          <summary>
            <span className="agent-report-name">{a.name ?? a.agent_id ?? a.agent_type}</span>
            <span className="agent-report-type">{a.agent_type}</span>
            <span className="agent-report-status">{rowLabel(a)}</span>
          </summary>
          <div className="agent-report-body">
            <MarkdownRenderer content={a.result || '(no report)'} />
            {a.agent_id && (
              <div className="agent-report-record">Full record: task_output {a.agent_id}</div>
            )}
          </div>
        </details>
      ))}
    </div>
  );
};
