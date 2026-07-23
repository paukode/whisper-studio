import { describe, expect, it } from 'vitest';
import {
  applyTeamProgressToMessage,
  findMatchingTeamReport,
  findMatchingTeamReports,
  foldTeamProgressIntoMap,
  foldTeamResultsInto,
  spawnLabel,
} from './teamProgress';
import type { ChatState } from '@/stores/chatStore';
import type { ChatMessage, TeamProgressEvent, ToolUseEvent } from '@/types/chat';

type Store = ChatState;

/** Minimal mutable stand-in for the chat store — only `messages` and
 *  `setMessages` are touched by applyTeamProgressToMessage. */
function makeStore(initial: ChatMessage[]) {
  const store = {
    messages: initial as ChatMessage[],
    setMessages(next: ChatMessage[]) { this.messages = next; },
  };
  return { getter: () => store as unknown as Store, current: () => store.messages };
}

describe('foldTeamResultsInto', () => {
  it('builds a completed team report from a fresh payload', () => {
    const out = foldTeamResultsInto(undefined, {
      team_id: 't1',
      team_name: 'Research Team',
      agents: [
        { name: 'a1', task: 'investigate', result: 'done', status: 'completed' },
      ],
    });
    expect(out.t1.team_name).toBe('Research Team');
    expect(out.t1.status).toBe('completed');
    expect(out.t1.agentOrder).toEqual(['a1']);
    expect(out.t1.agents.a1.result).toBe('done');
    expect(out.t1.agents.a1.status).toBe('completed');
  });

  it('maps error/failed status onto the failed terminal state', () => {
    const out = foldTeamResultsInto(undefined, {
      team_id: 't2',
      agents: [{ name: 'x', status: 'error' }],
    });
    expect(out.t2.agents.x.status).toBe('failed');
  });

  it('returns a copy of prior unchanged for a non-object payload', () => {
    const prior = { t9: { team_id: 't9', team_name: 'P', status: 'completed' as const, agents: {}, agentOrder: [] } };
    const out = foldTeamResultsInto(prior, null);
    expect(out).toEqual(prior);
    expect(out).not.toBe(prior); // shallow copy, not the same reference
  });

  it('ignores a payload with no team_id', () => {
    expect(foldTeamResultsInto(undefined, { agents: [{ name: 'a' }] })).toEqual({});
  });

  it('preserves live-fold timestamps when the final payload lands', () => {
    let map = foldTeamProgressIntoMap(undefined, {
      phase: 'team_started', team_id: 't8', team_name: 'x', agents: [],
    } as TeamProgressEvent)!;
    map = foldTeamProgressIntoMap(map, {
      phase: 'team_completed', team_id: 't8',
    } as TeamProgressEvent)!;
    const out = foldTeamResultsInto(map, { team_id: 't8', agents: [] });
    expect(out.t8.started_at).toBe(map.t8.started_at);
    expect(out.t8.completed_at).toBe(map.t8.completed_at);
  });

  it('merges a second result into an existing team without losing agents', () => {
    const first = foldTeamResultsInto(undefined, {
      team_id: 't3',
      team_name: 'T',
      agents: [{ name: 'a1', result: 'r1', status: 'completed' }],
    });
    const second = foldTeamResultsInto(first, {
      team_id: 't3',
      agents: [{ name: 'a2', result: 'r2', status: 'completed' }],
    });
    expect(second.t3.agentOrder).toEqual(['a1', 'a2']);
    expect(second.t3.agents.a1.result).toBe('r1');
    expect(second.t3.agents.a2.result).toBe('r2');
  });
});

describe('foldTeamProgressIntoMap', () => {
  it('builds the scaffold from team_started and tracks per-agent status', () => {
    const scaffold = foldTeamProgressIntoMap(undefined, {
      phase: 'team_started', team_id: 't1', team_name: 'audit',
      agents: [
        { name: 'a', task: 'ta', agent_type: 'explore', role: 'team' },
        { name: 'b', task: 'tb', agent_type: 'verify', role: 'team' },
      ],
    } as TeamProgressEvent)!;
    expect(scaffold.t1.status).toBe('running');
    expect(scaffold.t1.started_at).toBeTypeOf('number');
    expect(scaffold.t1.agentOrder).toEqual(['a', 'b']);
    expect(scaffold.t1.agents.a.status).toBe('pending');

    const running = foldTeamProgressIntoMap(scaffold, {
      phase: 'tool_call', team_id: 't1', agent_name: 'a', tool_name: 'ws_grep',
    } as TeamProgressEvent)!;
    expect(running.t1.agents.a.status).toBe('running');
    expect(running.t1.agents.b.status).toBe('pending');
    // Pure: the prior map is untouched.
    expect(scaffold.t1.agents.a.status).toBe('pending');
  });

  it('ignores team-less events and synthesizes a scaffold for early events', () => {
    expect(foldTeamProgressIntoMap(undefined, {
      phase: 'tool_call', agent_name: 'x',
    } as TeamProgressEvent)).toBeNull();

    const early = foldTeamProgressIntoMap(undefined, {
      phase: 'started', team_id: 't2', agent_id: 'abc123', task: 'full task text',
    } as TeamProgressEvent)!;
    expect(early.t2.agents.abc123.task).toBe('full task text');
  });

  it('maps the stopped phase onto the stopped status', () => {
    const out = foldTeamProgressIntoMap(undefined, {
      phase: 'stopped', team_id: 't3', agent_name: 'a',
    } as TeamProgressEvent)!;
    expect(out.t3.agents.a.status).toBe('stopped');
  });

  it('latches parent_agent_id so child rows stay badged', () => {
    let map = foldTeamProgressIntoMap(undefined, {
      phase: 'started', team_id: 't4', agent_id: 'child1', parent_agent_id: 'dad',
    } as TeamProgressEvent)!;
    map = foldTeamProgressIntoMap(map, {
      phase: 'turn_start', team_id: 't4', agent_id: 'child1', turn: 1,
    } as TeamProgressEvent)!;
    expect(map.t4.agents.child1.parent_agent_id).toBe('dad');
  });

  it('flips team status and stamps completed_at on team_completed', () => {
    let map = foldTeamProgressIntoMap(undefined, {
      phase: 'team_started', team_id: 't5', team_name: 'x', agents: [],
    } as TeamProgressEvent)!;
    map = foldTeamProgressIntoMap(map, {
      phase: 'team_completed', team_id: 't5',
    } as TeamProgressEvent)!;
    expect(map.t5.status).toBe('completed');
    expect(map.t5.completed_at).toBeTypeOf('number');
  });

  it('caps retained events per agent', () => {
    let map = foldTeamProgressIntoMap(undefined, {
      phase: 'started', team_id: 't6', agent_id: 'a1',
    } as TeamProgressEvent)!;
    for (let i = 0; i < 450; i++) {
      map = foldTeamProgressIntoMap(map, {
        phase: 'turn_start', team_id: 't6', agent_id: 'a1', turn: i,
      } as TeamProgressEvent)!;
    }
    expect(map.t6.agents.a1.events.length).toBeLessThanOrEqual(400);
  });
});

describe('findMatchingTeamReport', () => {
  const report = {
    team_id: 'abc', team_name: 'audit', status: 'running' as const,
    agents: {}, agentOrder: [],
  };

  it('matches by team_id from the team_create result JSON', () => {
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'team_create', status: 'complete',
      input: { team_name: 'other' }, result: JSON.stringify({ team_id: 'abc' }),
    }];
    expect(findMatchingTeamReport(tools, { abc: report })?.team_id).toBe('abc');
  });

  it('falls back to team_name while the tool is still running', () => {
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'team_create', status: 'running',
      input: { team_name: 'audit' },
    }];
    expect(findMatchingTeamReport(tools, { abc: report })?.team_id).toBe('abc');
  });

  it('returns null when nothing matches', () => {
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'spawn_agent', status: 'complete', input: {},
    }];
    expect(findMatchingTeamReport(tools, { abc: report })).toBeNull();
  });
});

describe('findMatchingTeamReports (spawn_agent one-member teams)', () => {
  const spawnReport = (id: string, name: string) => ({
    team_id: id, team_name: name, status: 'running' as const,
    agents: {}, agentOrder: [],
  });

  it('matches a finished spawn_agent by team_id in its result JSON', () => {
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'spawn_agent', status: 'complete',
      input: { task: 'audit the code' },
      result: JSON.stringify({ team_id: 's1', status: 'completed' }),
    }];
    const out = findMatchingTeamReports(tools, { s1: spawnReport('s1', 'audit the code') });
    expect(out.map((r) => r.team_id)).toEqual(['s1']);
  });

  it('matches a RUNNING spawn_agent by the spawn label derived from its task', () => {
    const task = 'Catalog the docs\nsecond line ignored';
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'spawn_agent', status: 'running', input: { task },
    }];
    const out = findMatchingTeamReports(tools, { s2: spawnReport('s2', 'Catalog the docs') });
    expect(out.map((r) => r.team_id)).toEqual(['s2']);
  });

  it('matches label with the 60-char ellipsis truncation', () => {
    const task = 'x'.repeat(100);
    const label = spawnLabel(task);
    expect(label.endsWith('…')).toBe(true);
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'spawn_agent', status: 'running', input: { task },
    }];
    const out = findMatchingTeamReports(tools, { s3: spawnReport('s3', label) });
    expect(out.map((r) => r.team_id)).toEqual(['s3']);
  });

  it('returns one report per spawn in a multi-spawn group, in tool order', () => {
    const tools: ToolUseEvent[] = [
      { toolId: '1', toolName: 'spawn_agent', status: 'complete', input: { task: 'a' }, result: JSON.stringify({ team_id: 'sa' }) },
      { toolId: '2', toolName: 'spawn_agent', status: 'complete', input: { task: 'b' }, result: JSON.stringify({ team_id: 'sb' }) },
    ];
    const reports = { sb: spawnReport('sb', 'b'), sa: spawnReport('sa', 'a') };
    expect(findMatchingTeamReports(tools, reports).map((r) => r.team_id)).toEqual(['sa', 'sb']);
  });

  it('skips detached spawns (background-task card, no scaffold)', () => {
    const tools: ToolUseEvent[] = [{
      toolId: '1', toolName: 'spawn_agent', status: 'complete',
      input: { task: 'bg work', detach: true },
    }];
    expect(findMatchingTeamReports(tools, { s4: spawnReport('s4', 'bg work') })).toEqual([]);
  });

  it('same-name reports map to distinct spawns instead of duplicating', () => {
    const tools: ToolUseEvent[] = [
      { toolId: '1', toolName: 'spawn_agent', status: 'running', input: { task: 'same' } },
      { toolId: '2', toolName: 'spawn_agent', status: 'running', input: { task: 'same' } },
    ];
    const reports = { p1: spawnReport('p1', 'same'), p2: spawnReport('p2', 'same') };
    const out = findMatchingTeamReports(tools, reports);
    expect(new Set(out.map((r) => r.team_id)).size).toBe(2);
  });
});

describe('applyTeamProgressToMessage', () => {
  const SUB_TS = '2026-06-07T00:00:00.000Z';

  it('folds events into the message matched by timestamp, even when it is not last', () => {
    const { getter, current } = makeStore([
      { role: 'user', content: 'do it', timestamp: 'u1' },
      { role: 'assistant', content: '', timestamp: SUB_TS },
      // A later turn the user started while the subagent runs — the subagent
      // message is no longer last, but progress must still land on it.
      { role: 'user', content: 'meanwhile', timestamp: 'u2' },
    ]);

    applyTeamProgressToMessage(getter, SUB_TS, {
      phase: 'team_started', team_id: 'sub', team_name: 'Subagent',
      agents: [{ name: 'Subagent', task: 't', agent_type: 'general', role: 'team' }],
    } as TeamProgressEvent);
    applyTeamProgressToMessage(getter, SUB_TS, {
      phase: 'tool_call', team_id: 'sub', agent_name: 'Subagent', tool_name: 'web_fetch',
    } as TeamProgressEvent);

    const target = current().find((m) => m.timestamp === SUB_TS)!;
    expect(target.teamReports?.sub).toBeDefined();
    expect(target.teamReports!.sub.agents.Subagent.status).toBe('running');
    expect(target.teamReports!.sub.agents.Subagent.events.length).toBe(1);
    // The other messages are untouched.
    expect(current().find((m) => m.timestamp === 'u2')!.teamReports).toBeUndefined();
  });

  it('ignores events for a timestamp that does not exist', () => {
    const { getter, current } = makeStore([
      { role: 'assistant', content: '', timestamp: SUB_TS },
    ]);
    applyTeamProgressToMessage(getter, 'missing', {
      phase: 'team_started', team_id: 'x', agents: [],
    } as TeamProgressEvent);
    expect(current()[0].teamReports).toBeUndefined();
  });

  it('folds into the assistant message when a user message shares the timestamp', () => {
    // The /subagent handler can add a user + assistant message on the same
    // millisecond; progress must still land on the assistant message.
    const { getter, current } = makeStore([
      { role: 'user', content: 'do it', timestamp: SUB_TS },
      { role: 'assistant', content: '', timestamp: SUB_TS },
    ]);
    applyTeamProgressToMessage(getter, SUB_TS, {
      phase: 'team_started', team_id: 'subagent-x', team_name: 'Subagent',
      agents: [{ name: 'Subagent', task: 't', agent_type: 'general', role: 'team' }],
    } as TeamProgressEvent);
    expect(current().find((m) => m.role === 'user')!.teamReports).toBeUndefined();
    expect(current().find((m) => m.role === 'assistant')!.teamReports?.['subagent-x']).toBeDefined();
  });

  it('ignores a non-assistant target message', () => {
    const { getter, current } = makeStore([
      { role: 'user', content: 'x', timestamp: SUB_TS },
    ]);
    applyTeamProgressToMessage(getter, SUB_TS, {
      phase: 'team_started', team_id: 'x', agents: [],
    } as TeamProgressEvent);
    expect(current()[0].teamReports).toBeUndefined();
  });
});
