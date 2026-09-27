import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import type { ToolUseEvent } from '@/types/chat';
import {
  STOPPED_TOOL_RESULT,
  killSessionStream,
  registerStreamController,
  wasKillFinalized,
} from './streamControl';

// The ESC kill switch must also stop the shell work the stopped stream
// started, and only that: the request carries when the stream began, so the
// backend spares a dev server an earlier turn left running. Fire-and-forget,
// never blocking or breaking the synchronous kill.
describe('killSessionStream: shell-work stop wire', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stopBody(): { session_id: string; since: number } {
    const call = fetchMock.mock.calls.find((c) => c[0] === '/api/workspace/shell/tasks/stop');
    expect(call).toBeDefined();
    return JSON.parse((call![1] as RequestInit).body as string);
  }

  it('sends the session id and the running stream start as `since`', () => {
    const before = Date.now() / 1000;
    registerStreamController('sess-123', new AbortController());
    const after = Date.now() / 1000;

    killSessionStream('sess-123');

    const body = stopBody();
    expect(body.session_id).toBe('sess-123');
    expect(body.since).toBeGreaterThanOrEqual(before);
    expect(body.since).toBeLessThanOrEqual(after);
  });

  it('scopes `since` to the turn actually running, not an earlier one', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(1_000_000);
      registerStreamController('sess-cont', new AbortController(), { startsTurn: true });
      // A fresh send starts a new turn and replaces the controller.
      vi.setSystemTime(1_060_000);
      registerStreamController('sess-cont', new AbortController(), { startsTurn: true });

      killSessionStream('sess-cont');
      expect(stopBody().since).toBe(1_060);
    } finally {
      vi.useRealTimers();
    }
  });

  it('every later leg of a turn keeps its start, so the work of earlier legs stays in reach', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(2_000_000);
      registerStreamController('sess-appr', new AbortController(), { startsTurn: true });
      // The leg that runs the approved action registers when the user clicks
      // approve, and the continuation after the action returns: both are legs
      // of the same turn, whatever their call site passes.
      vi.setSystemTime(2_030_000);
      registerStreamController('sess-appr', new AbortController());
      killSessionStream('sess-appr');
      expect(stopBody().since).toBe(2_000);

      fetchMock.mockClear();
      vi.setSystemTime(2_045_000);
      registerStreamController('sess-appr', new AbortController());
      killSessionStream('sess-appr');
      expect(stopBody().since).toBe(2_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a leg with no turn on record starts one', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(3_000_000);
      registerStreamController('sess-fresh-leg', new AbortController());
      killSessionStream('sess-fresh-leg');
      expect(stopBody().since).toBe(3_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not reach for tasks when no stream is running in the session', () => {
    killSessionStream('sess-idle');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not fire for a null session', () => {
    killSessionStream(null);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('stays synchronous and quiet when the request fails', () => {
    fetchMock.mockRejectedValue(new Error('offline'));
    registerStreamController('sess-offline', new AbortController());
    expect(() => killSessionStream('sess-offline')).not.toThrow();
  });
});

// The ESC kill switch must reach any controller currently registered for the
// session — including an auto-approved continuation leg, which now always
// registers its own controller (regression guard for the "continuation
// unstoppable after the outer stream ended" bug).
describe('killSessionStream — aborts the registered controller', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('aborts and kill-finalizes a controller registered for the session', () => {
    const controller = new AbortController();
    registerStreamController('sess-abrt', controller);
    expect(controller.signal.aborted).toBe(false);

    killSessionStream('sess-abrt');

    expect(controller.signal.aborted).toBe(true);
    expect(wasKillFinalized(controller)).toBe(true);
  });
});

// Regression: pressing Stop / ESC mid-turn used to drop every tool step the
// live trace had shown (the "(Stopped)" message carried no toolUse and
// finishStream cleared currentStreamToolUse in the same pass). The kill must
// commit the activity shown so far, with running steps frozen as 'stopped'.
describe('killSessionStream — keeps the tool activity shown so far', () => {
  const readStep: ToolUseEvent = {
    toolId: 'ws_read_file',
    toolName: 'ws_read_file',
    input: { path: '/repo/src/a.ts' },
    result: 'export const a = 1;',
    status: 'complete',
  };
  const grepStep: ToolUseEvent = {
    toolId: 'ws_grep',
    toolName: 'ws_grep',
    input: { pattern: 'TODO' },
    status: 'running',
  };
  let sid: string;
  let n = 0;

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true } as Response));
    sid = `sess-tools-${++n}`;
    getChatStore(sid).setState({
      isStreaming: true,
      currentStreamContent: '',
      currentStreamToolUse: [readStep, grepStep],
    });
    registerStreamController(sid, new AbortController());
  });

  afterEach(() => {
    dropRuntime(sid);
    vi.unstubAllGlobals();
  });

  it('commits one (Stopped) message carrying every step, the running one frozen', () => {
    killSessionStream(sid);

    const s = getChatStore(sid).getState();
    expect(s.isStreaming).toBe(false);
    expect(s.currentStreamToolUse).toEqual([]);
    const assistant = s.messages.filter((m) => m.role === 'assistant');
    expect(assistant).toHaveLength(1);
    const msg = assistant[0];
    expect(msg.content).toBe('*(Stopped)*');
    expect(msg.stopped).toBe(true);
    expect(msg.toolUse).toHaveLength(2);
    expect(msg.toolUse?.[0]).toEqual(readStep);
    expect(msg.toolUse?.[1]).toMatchObject({
      toolName: 'ws_grep',
      status: 'stopped',
      result: STOPPED_TOOL_RESULT,
    });
  });

  it('rides the same toolUse on the partial-prose message', () => {
    getChatStore(sid).setState({ currentStreamContent: 'Looking at the file' });

    killSessionStream(sid);

    const s = getChatStore(sid).getState();
    expect(s.isStreaming).toBe(false);
    const assistant = s.messages.filter((m) => m.role === 'assistant');
    expect(assistant).toHaveLength(1);
    expect(assistant[0].content).toBe('Looking at the file\n\n*(Stopped)*');
    expect(assistant[0].stopped).toBe(true);
    expect(assistant[0].toolUse).toHaveLength(2);
    expect(assistant[0].toolUse?.[1].status).toBe('stopped');
  });

  it('appends nothing when the turn had shown neither prose nor tools', () => {
    getChatStore(sid).setState({ currentStreamToolUse: [] });

    killSessionStream(sid);

    const s = getChatStore(sid).getState();
    expect(s.isStreaming).toBe(false);
    expect(s.messages.filter((m) => m.role === 'assistant')).toHaveLength(0);
  });

  it('closes a live team card: running agents stopped, team completed', () => {
    const chat = getChatStore(sid).getState();
    chat.foldTeamEvent({
      phase: 'team_started',
      team_id: 'team-1',
      team_name: 'Research',
      agents: [
        { name: 'reader', task: 'read', agent_type: 'general' },
        { name: 'writer', task: 'write', agent_type: 'general' },
      ],
    });
    chat.foldTeamEvent({ phase: 'started', team_id: 'team-1', agent_name: 'reader', agent_id: 'a1' });
    chat.foldTeamEvent({ phase: 'completed', team_id: 'team-1', agent_name: 'reader', agent_id: 'a1' });
    chat.foldTeamEvent({ phase: 'started', team_id: 'team-1', agent_name: 'writer', agent_id: 'a2' });

    killSessionStream(sid);

    const s = getChatStore(sid).getState();
    expect(s.liveTeamReports).toEqual({});
    const msg = s.messages.filter((m) => m.role === 'assistant')[0];
    const report = msg.teamReports?.['team-1'];
    expect(report?.status).toBe('completed');
    expect(report?.completed_at).toBeDefined();
    expect(report?.agents.reader.status).toBe('completed');
    expect(report?.agents.writer.status).toBe('stopped');
    expect(report?.agents.writer.events[report.agents.writer.events.length - 1]?.phase).toBe('stopped');
  });
});
