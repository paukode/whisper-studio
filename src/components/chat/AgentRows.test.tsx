/**
 * Render contract for the chat's agent rows: one line per agent with its
 * state, and Open only for an agent that has a record to show.
 */
import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { AgentRow, TeamAgentRows, agentStatusText } from './AgentRows';
import { useDockStore } from '@/stores/dockStore';
import type { TeamAgentReport, TeamReportData } from '@/types/chat';

function agent(over: Partial<TeamAgentReport> = {}): TeamAgentReport {
  return { name: 'Probe', task: 'probe the org', agent_type: 'general', role: 'team', status: 'running', agent_id: 'a1', ...over };
}

describe('AgentRow', () => {
  beforeEach(() => {
    useDockStore.setState({ panels: [], sizes: [], open: false });
  });

  it('shows the state and opens the agent pane in the dock', () => {
    const { getByText } = render(<AgentRow agent={agent()} />);
    expect(getByText('Probe')).toBeTruthy();
    expect(getByText(/running/)).toBeTruthy();
    fireEvent.click(getByText('Open'));
    const { panels, open } = useDockStore.getState();
    expect(open).toBe(true);
    expect(panels.map((p) => [p.id, p.kind, p.title])).toEqual([['agent:a1', 'agent', 'Probe']]);
  });

  it('offers no Open for a cron run or an agent without an id', () => {
    const cron = render(<AgentRow agent={agent({ agent_type: 'cron', agent_id: 'cron:r1' })} />);
    expect(cron.queryByText('Open')).toBeNull();
    const noId = render(<AgentRow agent={agent({ agent_id: undefined })} />);
    expect(noId.queryByText('Open')).toBeNull();
  });

  it('names the outcome and the rounds spent', () => {
    expect(agentStatusText(agent({ status: 'completed', turns_used: 5 }))).toBe('done · 5 rounds');
    expect(agentStatusText(agent({ status: 'completed', turns_used: 1 }))).toBe('done · 1 round');
    expect(agentStatusText(agent({ status: 'turn_limit', stop_reason: 'deadline', turns_used: 9 }))).toBe('stopped at the time limit · 9 rounds');
    expect(agentStatusText(agent({ status: 'stopped', stop_reason: 'cancelled' }))).toBe('stopped');
    expect(agentStatusText(agent({ status: 'pending' }))).toBe('starting');
  });
});

describe('TeamAgentRows', () => {
  it('names the team above its members when there are several', () => {
    const report: TeamReportData = {
      team_id: 't1',
      team_name: 'audit',
      status: 'running',
      agents: { a: agent({ name: 'a', status: 'completed' }), b: agent({ name: 'b', agent_id: 'b1' }) },
      agentOrder: ['a', 'b'],
    };
    const { getByText, getAllByTestId } = render(<TeamAgentRows report={report} />);
    expect(getByText(/Team · audit · 1\/2 done/)).toBeTruthy();
    expect(getAllByTestId('agent-row')).toHaveLength(2);
  });

  it('shows one member without a team line', () => {
    const report: TeamReportData = {
      team_id: 't2', team_name: 'solo', status: 'running', agents: { a: agent({ name: 'a' }) }, agentOrder: ['a'],
    };
    const { queryByText } = render(<TeamAgentRows report={report} />);
    expect(queryByText(/Team ·/)).toBeNull();
  });
});
