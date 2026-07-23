/**
 * TeamReportCard — rich, expandable rendering for a team_create run.
 *
 * Replaces the generic AgentCard / JSON-dump view when a TeamReportData
 * payload has been folded onto the message (by useChatStream applying
 * `team_progress` SSE events). Each spawned worker gets its own
 * collapsible row showing name, task, role badge, agent_type and live
 * status; expanding the row reveals the real-time event log plus the
 * agent's terminal output.
 */

import React from 'react';
import type { TeamReportData, TeamAgentReport, TeamProgressEvent } from '@/types/chat';
import { useSubagentStore } from '@/stores/subagentStore';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';

interface Props {
  report: TeamReportData;
}

function roleLabel(role: TeamAgentReport['role']): string {
  return role === 'orchestrator' ? 'orchestrator' : 'team';
}

function statusGlyph(status: TeamAgentReport['status']): React.ReactNode {
  if (status === 'completed') return <span className="trace-check">{'✓'}</span>;
  if (status === 'failed') return <span className="trace-check" style={{ color: 'var(--error, #f87171)' }}>{'✕'}</span>;
  if (status === 'turn_limit') return <span className="trace-check" style={{ color: 'var(--warning, #fbbf24)' }}>{'⚠'}</span>;
  if (status === 'stopped') return <span className="trace-check" style={{ color: 'var(--text-muted)' }}>{'⏹'}</span>;
  if (status === 'running') return <span className="trace-spinner">{'⟳'}</span>;
  return <span className="trace-spinner" style={{ opacity: 0.5 }}>{'⟳'}</span>;
}

function statusLabel(a: TeamAgentReport): string {
  const tools = a.events.filter(e => e.phase === 'tool_call').length;
  const toolsSuffix = tools > 0 ? ` · ${tools} tool${tools === 1 ? '' : 's'}` : '';
  if (a.status === 'completed') {
    const turns = a.turns_used ? `done · ${a.turns_used} turn${a.turns_used === 1 ? '' : 's'}` : 'done';
    return turns + toolsSuffix;
  }
  if (a.status === 'failed') return 'failed';
  if (a.status === 'stopped') return 'stopped' + toolsSuffix;
  if (a.status === 'turn_limit') return `stopped · ${a.turns_used ?? '?'} turns${toolsSuffix}`;
  if (a.status === 'running') {
    // Use the most recent turn_start event's turn number, if any.
    const lastTurn = [...a.events].reverse().find(e => typeof e.turn === 'number')?.turn;
    return lastTurn ? `running · turn ${lastTurn}` : 'running';
  }
  return 'pending';
}

/** Compact elapsed display from the report's client-side receipt times.
 *  While running, measures against now — the card re-renders on every
 *  folded event, so this stays fresh enough without a timer. */
function elapsedLabel(report: TeamReportData): string | null {
  if (!report.started_at) return null;
  const end = report.completed_at ?? Date.now();
  const s = Math.max(0, Math.round((end - report.started_at) / 1000));
  if (s < 1) return null;
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function teamStatusLabel(report: TeamReportData): string {
  const ags = Object.values(report.agents);
  const done = ags.filter(a => a.status === 'completed').length;
  const total = report.agentOrder.length || ags.length || 0;
  const elapsed = elapsedLabel(report);
  const suffix = elapsed ? ` · ${elapsed}` : '';
  if (report.status === 'completed') return `${done}/${total} done${suffix}`;
  const running = ags.filter(a => a.status === 'running').length;
  return running > 0
    ? `${done}/${total} done · ${running} running${suffix}`
    : `${done}/${total} done${suffix}`;
}

function teamStatusGlyph(report: TeamReportData): React.ReactNode {
  const ags = Object.values(report.agents);
  const failed = ags.filter(a => a.status === 'failed').length;
  if (report.status === 'completed') {
    return failed > 0
      ? <span className="trace-check" style={{ color: 'var(--error, #f87171)' }}>{'✕'}</span>
      : <span className="trace-check">{'✓'}</span>;
  }
  return <span className="trace-spinner">{'⟳'}</span>;
}

function EventRow({ ev }: { ev: TeamProgressEvent }) {
  // Compact one-line event renderer used inside the per-agent live log.
  switch (ev.phase) {
    case 'started':
      return (
        <div className="team-event team-event-meta">
          <span className="team-event-icon">{'\u{1F680}'}</span>
          <span>started{ev.model ? ` · ${ev.model}` : ''}{ev.max_turns ? ` · max ${ev.max_turns} turns` : ''}</span>
        </div>
      );
    case 'turn_start':
      return (
        <div className="team-event team-event-turn">
          <span className="team-event-icon">{'·'}</span>
          <span>turn {ev.turn}</span>
        </div>
      );
    case 'text':
      return (
        <div className="team-event team-event-text">
          <span className="team-event-icon">{'\u{1F4AC}'}</span>
          <div className="team-event-pre">
            <MarkdownRenderer content={ev.text ?? ''} stepFormat />
          </div>
        </div>
      );
    case 'tool_call': {
      // Collapsed line shows the short preview; when the runtime shipped the
      // full input (and it says more than the preview), the line becomes a
      // click-to-expand row per the "all tool calls visible" contract.
      const fullInput = ev.tool_input_full && ev.tool_input_full.length > (ev.tool_input_preview?.length ?? 0)
        ? ev.tool_input_full : null;
      const callRow = (
        <>
          <span className="team-event-icon">{'▸'}</span>
          <span className="team-event-tool-name">{ev.tool_name}</span>
          {ev.tool_input_preview && (
            <span className="team-event-tool-args">{ev.tool_input_preview}</span>
          )}
        </>
      );
      if (!fullInput) {
        return <div className="team-event team-event-tool-call">{callRow}</div>;
      }
      return (
        <details className="team-event team-event-tool-call team-event-expandable">
          <summary style={{ display: 'flex', alignItems: 'baseline', gap: 6, cursor: 'pointer', listStyle: 'none' }} title="Show full input">
            {callRow}
          </summary>
          <pre className="team-event-pre team-event-full">{fullInput}</pre>
        </details>
      );
    }
    case 'tool_result': {
      const fullOutput = ev.output_full && ev.output_full.length > (ev.output_preview?.length ?? 0)
        ? ev.output_full : null;
      const resultRow = (
        <>
          <span className="team-event-icon">{ev.status === 'error' ? '✕' : '✓'}</span>
          <span className="team-event-tool-name">{ev.tool_name}</span>
          {ev.output_preview && (
            <span className="team-event-tool-args">{ev.output_preview}</span>
          )}
        </>
      );
      if (!fullOutput) {
        return (
          <div className={`team-event team-event-tool-result ${ev.status === 'error' ? 'errored' : ''}`}>
            {resultRow}
          </div>
        );
      }
      return (
        <details className={`team-event team-event-tool-result team-event-expandable ${ev.status === 'error' ? 'errored' : ''}`}>
          <summary style={{ display: 'flex', alignItems: 'baseline', gap: 6, cursor: 'pointer', listStyle: 'none' }} title="Show full output">
            {resultRow}
          </summary>
          <pre className="team-event-pre team-event-full">{fullOutput}</pre>
        </details>
      );
    }
    case 'completed':
      return (
        <div className="team-event team-event-done">
          <span className="team-event-icon">{'✓'}</span>
          <span>completed{ev.turns_used ? ` · ${ev.turns_used} turns` : ''}</span>
        </div>
      );
    case 'turn_limit':
      return (
        <div className="team-event team-event-warn">
          <span className="team-event-icon">{'⚠'}</span>
          <span>reached turn limit ({ev.turns_used} turns)</span>
        </div>
      );
    case 'failed':
      return (
        <div className="team-event team-event-error">
          <span className="team-event-icon">{'✕'}</span>
          <span>failed{ev.error ? `: ${ev.error}` : ''}</span>
        </div>
      );
    case 'stopped':
      return (
        <div className="team-event team-event-meta">
          <span className="team-event-icon">{'⏹'}</span>
          <span>stopped by user</span>
        </div>
      );
    default:
      return null;
  }
}

export function AgentRow({ agent }: { agent: TeamAgentReport }) {
  const open = agent.status === 'running' || agent.status === 'failed';
  return (
    <details className={`team-agent-row team-agent-${agent.status}`} open={open}>
      <summary className="team-agent-summary">
        <span className="team-agent-icon">{'\u{1F916}'}</span>
        <span className="team-agent-name">{agent.name}</span>
        <span className={`team-agent-badge team-agent-badge-${agent.role}`}>{roleLabel(agent.role)}</span>
        <span className="team-agent-badge team-agent-badge-type">{agent.agent_type}</span>
        {agent.parent_agent_id && (
          <span
            className="team-agent-badge team-agent-badge-type"
            title={`Spawned by agent ${agent.parent_agent_id}`}
          >
            {'↳ child'}
          </span>
        )}
        {agent.model && (
          <span className="team-agent-badge team-agent-badge-model">{agent.model}</span>
        )}
        <span className="team-agent-status">
          {statusGlyph(agent.status)}
          <span className="team-agent-status-text">{statusLabel(agent)}</span>
        </span>
      </summary>
      <div className="team-agent-body">
        {agent.task && (
          <div className="team-agent-task">
            <span className="team-agent-task-label">Task:</span>
            <span className="team-agent-task-text">{agent.task}</span>
          </div>
        )}
        {agent.events.length > 0 && (
          <div className="team-agent-log">
            {agent.events.map((ev, i) => (
              <EventRow key={i} ev={ev} />
            ))}
          </div>
        )}
        {agent.result && (
          <div className="team-agent-result">
            <div className="team-agent-result-label">Final output</div>
            <pre className="team-agent-result-pre">{agent.result}</pre>
          </div>
        )}
        {agent.events.length === 0 && !agent.result && (
          <div className="team-agent-empty">no activity yet</div>
        )}
      </div>
    </details>
  );
}

export function TeamReportCard({ report }: Props) {
  const allDone = report.status === 'completed';
  // Background /subagent runs register a client-side stop handler (aborting
  // their SSE stream). team_create runs have no handler — for those the
  // button cancels the server-side gather via the stop endpoint, which
  // flips every running agent to "stopped" through the event stream.
  const stop = useSubagentStore((s) => s.stops[report.team_id]);
  const stopTeam = React.useCallback(() => {
    if (stop) {
      stop();
      return;
    }
    void fetch(`/api/teams/${encodeURIComponent(report.team_id)}/stop`, { method: 'POST' })
      .catch(() => { /* the card stays running; the user can hit ESC */ });
  }, [stop, report.team_id]);
  const orderedAgents = report.agentOrder
    .map(key => report.agents[key])
    .filter(Boolean);
  // Include any agents that arrived without team_started (defensive)
  for (const key of Object.keys(report.agents)) {
    if (!report.agentOrder.includes(key)) {
      orderedAgents.push(report.agents[key]);
    }
  }

  return (
    <details
      className={`team-report ${allDone ? 'done' : 'running'}`}
      open={!allDone}
    >
      <summary className="team-report-summary">
        <span className="team-report-icon" aria-hidden="true">{'\u{1F465}'}</span>
        <span className="team-report-title">
          Team: {report.team_name || '(unnamed)'}{' '}
          <span className="team-report-count">
            · {orderedAgents.length} agent{orderedAgents.length === 1 ? '' : 's'}
          </span>
        </span>
        <span className="team-report-status">
          {teamStatusLabel(report)} {teamStatusGlyph(report)}
        </span>
        {!allDone && (
          <button
            type="button"
            title="Stop this team"
            // Don't let the click toggle the <details> open/closed.
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); stopTeam(); }}
            style={{
              marginLeft: 8, flexShrink: 0, cursor: 'pointer',
              fontSize: 11, lineHeight: 1, padding: '3px 8px', borderRadius: 6,
              border: '1px solid var(--border)', background: 'var(--bg-surface)',
              color: 'var(--text-secondary)',
            }}
          >
            ⏹ Stop
          </button>
        )}
      </summary>
      <div className="team-report-body">
        {report.description && (
          <div className="team-report-description">{report.description}</div>
        )}
        {orderedAgents.map(agent => (
          <AgentRow key={agent.name} agent={agent} />
        ))}
      </div>
    </details>
  );
}
