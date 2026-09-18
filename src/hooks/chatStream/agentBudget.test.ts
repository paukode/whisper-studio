import { describe, it, expect } from 'vitest';
import { agentBudget, formatSeconds } from './agentBudget';
import type { TeamAgentReport } from '@/types/chat';

function row(over: Partial<TeamAgentReport>): TeamAgentReport {
  return {
    name: 'a',
    task: 't',
    agent_type: 'general',
    role: 'team',
    status: 'running',
    events: [],
    ...over,
  };
}

describe('agentBudget', () => {
  it('reads cap, rounds, time and cost from the latest turn_start event', () => {
    const b = agentBudget(
      row({
        events: [
          { phase: 'started', max_turns: 30, deadline_s: 600 },
          { phase: 'turn_start', turn: 2, max_turns: 30, elapsed_s: 12.5, deadline_s: 600, cost_usd: 0.05, budget_state: 'working' },
          { phase: 'turn_start', turn: 8, max_turns: 50, elapsed_s: 90, deadline_s: 900, cost_usd: 0.41, budget_state: 'working' },
        ],
      }),
    );
    expect(b.turns).toEqual({ used: 7, cap: 50 });
    expect(b.time).toEqual({ elapsed: 90, cap: 900 });
    expect(b.costUsd).toBe(0.41);
    expect(b.state).toBe('working');
  });

  it('shows finishing up while running the final round and names the limit afterwards', () => {
    const running = row({ events: [{ phase: 'turn_start', turn: 30, max_turns: 30, budget_state: 'finishing' }] });
    expect(agentBudget(running).state).toBe('finishing');
    const limited = row({ status: 'completed', stop_reason: 'cost_cap', turns_used: 12, events: [] });
    expect(agentBudget(limited)).toMatchObject({ state: 'limit', turns: { used: 12, cap: null } });
    const salvaged = row({ status: 'stopped', stop_reason: 'cancelled', events: [] });
    expect(agentBudget(salvaged).state).toBe('salvaged');
    const done = row({ status: 'completed', stop_reason: 'completed', turns_used: 4, events: [] });
    expect(agentBudget(done).state).toBe('done');
  });

  it('formats seconds compactly', () => {
    expect(formatSeconds(42)).toBe('42s');
    expect(formatSeconds(600)).toBe('10m');
    expect(formatSeconds(125)).toBe('2m05s');
  });
});
