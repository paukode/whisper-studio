/**
 * The chat's view of delegated work: one line per agent, the way the header's
 * task list shows it. Name, status, and Open, which puts the agent's full live
 * feed (its words, every tool call and result) in the dock's agent pane.
 * Shared by committed messages, the streaming turn, voice runs and workflow
 * run cards; Stop lives in the pane and in the header's task list.
 */
import React from 'react';
import type { TeamAgentReport, TeamReportData } from '@/types/chat';
import { useDockStore } from '@/stores/dockStore';
import { RobotIcon } from '@/components/common/icons';

const STATUS_TEXT: Record<TeamAgentReport['status'], string> = {
  pending: 'starting',
  running: 'running',
  completed: 'done',
  failed: 'failed',
  turn_limit: 'stopped at the turn limit',
  stopped: 'stopped',
};

const STOP_REASON_TEXT: Record<string, string> = {
  turn_limit: 'stopped at the turn limit',
  deadline: 'stopped at the time limit',
  cost_cap: 'stopped at the cost cap',
  refused_calls: 'stopped after repeated refused calls',
  cancelled: 'stopped',
};

export function agentStatusText(agent: TeamAgentReport): string {
  const reason = agent.stop_reason ? STOP_REASON_TEXT[agent.stop_reason] : undefined;
  const base = reason ?? STATUS_TEXT[agent.status];
  const rounds = agent.turns_used ? ` · ${agent.turns_used} round${agent.turns_used === 1 ? '' : 's'}` : '';
  return base + rounds;
}

/** A cron run folds into the same rows but is a turn, not an agent: it has
 *  no record to open. */
function openableId(agent: TeamAgentReport): string | null {
  return agent.agent_id && agent.agent_type !== 'cron' ? agent.agent_id : null;
}

export const AgentRow: React.FC<{ agent: TeamAgentReport }> = ({ agent }) => {
  const openAgent = useDockStore((s) => s.openAgent);
  const agentId = openableId(agent);
  return (
    <div className={`agent-row agent-row-${agent.status}`} data-testid="agent-row">
      <RobotIcon />
      <span className="agent-row-text">
        Agent · <span className="agent-row-name">{agent.name}</span> · {agentStatusText(agent)}
      </span>
      {agentId && (
        <button type="button" className="agent-row-open" onClick={() => openAgent(agentId, agent.name)}>
          Open
        </button>
      )}
    </div>
  );
};

export const TeamAgentRows: React.FC<{ report: TeamReportData }> = ({ report }) => {
  const agents = report.agentOrder.map((key) => report.agents[key]).filter(Boolean);
  for (const key of Object.keys(report.agents)) {
    if (!report.agentOrder.includes(key)) agents.push(report.agents[key]);
  }
  const done = agents.filter((a) => a.status === 'completed').length;
  return (
    <div className="agent-rows">
      {agents.length > 1 && (
        <div className="agent-rows-team">
          Team · {report.team_name || 'unnamed'} · {done}/{agents.length} done
        </div>
      )}
      {agents.map((agent) => (
        <AgentRow key={agent.name} agent={agent} />
      ))}
    </div>
  );
};
