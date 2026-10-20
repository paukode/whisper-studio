import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  formatSeconds,
  mergeEvents,
  metaAfter,
  recordReadout,
  useAgentFeedStore,
  type AgentProgressEvent,
  type AgentRecord,
  type AgentRecordMeta,
} from './agentFeedStore';

const meta = (over: Partial<AgentRecordMeta> = {}): AgentRecordMeta => ({
  agent_id: 'a1',
  task: 'probe',
  model: 'opus',
  max_turns: 120,
  status: 'running',
  started_at: '2026-10-18T10:00:00Z',
  rounds: 0,
  ...over,
});

const ev = (over: Partial<AgentProgressEvent>): AgentProgressEvent =>
  ({ agent_id: 'a1', phase: 'text', ...over }) as AgentProgressEvent;

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

describe('mergeEvents', () => {
  it('appends only events numbered past the record and keeps the array when nothing is new', () => {
    const held = [ev({ seq: 1 }), ev({ seq: 2 })];
    expect(mergeEvents(held, [ev({ seq: 2 }), ev({ seq: 1 })])).toBe(held);
    const next = mergeEvents(held, [ev({ seq: 2 }), ev({ seq: 3 }), ev({ seq: 3 }), ev({ seq: 4 })]);
    expect(next.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(held).toHaveLength(2);
  });

  it('ignores notices that carry no seq', () => {
    const held = [ev({ seq: 1 })];
    expect(mergeEvents(held, [ev({ phase: 'stopped' })])).toBe(held);
  });
});

describe('metaAfter', () => {
  it('follows rounds and the terminal phases', () => {
    let m = metaAfter(meta(), ev({ phase: 'turn_start', turn: 5, max_turns: 140 }));
    expect(m).toMatchObject({ status: 'running', rounds: 4, max_turns: 140 });
    m = metaAfter(m, ev({ phase: 'stopped' }));
    expect(m).toMatchObject({ status: 'stopped', stop_reason: 'cancelled' });
    m = metaAfter(meta(), ev({ phase: 'turn_limit', stop_reason: 'deadline', turns_used: 9 }));
    expect(m).toMatchObject({ status: 'completed', stop_reason: 'deadline', rounds: 9 });
    m = metaAfter(meta(), ev({ phase: 'finished', status: 'failed', stop_reason: 'error', turns_used: 2 }));
    expect(m).toMatchObject({ status: 'failed', stop_reason: 'error', rounds: 2 });
  });
});

describe('recordReadout', () => {
  const rec = (over: Partial<AgentRecord>): AgentRecord => ({
    meta: meta(),
    events: [],
    report: '',
    sharedBrief: null,
    loading: false,
    error: null,
    pending: [],
    ...over,
  });

  it('reads the status line off the last round and the clock', () => {
    const r = rec({
      events: [ev({ phase: 'turn_start', seq: 3, turn: 4, max_turns: 120, cost_usd: 0.42, budget_state: 'working' })],
    });
    const out = recordReadout(r, Date.parse('2026-10-18T10:03:12Z'));
    expect(out).toEqual({
      status: 'running',
      running: true,
      model: 'opus',
      rounds: 3,
      roundsCap: 120,
      elapsedS: 192,
      costUsd: 0.42,
    });
  });

  it('names the final round and the stop reason', () => {
    const finishing = rec({ events: [ev({ phase: 'turn_start', seq: 1, turn: 120, budget_state: 'finishing' })] });
    expect(recordReadout(finishing, 0).status).toBe('finishing up');
    const capped = rec({ meta: meta({ status: 'completed', stop_reason: 'cost_cap', finished_at: '2026-10-18T10:01:00Z' }) });
    expect(recordReadout(capped, 0)).toMatchObject({ status: 'stopped at the cost cap', running: false, elapsedS: 60 });
    const failed = rec({ meta: meta({ status: 'failed', stop_reason: 'error' }) });
    expect(recordReadout(failed, 0).status).toBe('failed');
  });
});

describe('formatSeconds', () => {
  it('reads seconds, then minutes, and carries a rounded-up second into the minute', () => {
    expect(formatSeconds(42.4)).toBe('42s');
    expect(formatSeconds(471)).toBe('7m 51s');
    expect(formatSeconds(480)).toBe('8m');
    expect(formatSeconds(59.6)).toBe('1m');
    expect(formatSeconds(119.6)).toBe('2m');
  });
});

describe('agentFeedStore', () => {
  beforeEach(() => {
    useAgentFeedStore.setState({ records: {} });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('reads the record once and follows it live by seq', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          meta: meta(),
          events: [ev({ seq: 1, phase: 'started' })],
          report: '',
          shared_brief: { text: 'BRIEF\n', agents: 2 },
        }),
      }),
    );
    await useAgentFeedStore.getState().open('a1');
    useAgentFeedStore.getState().applyLive(ev({ seq: 2, phase: 'tool_call', tool_name: 'ws_grep' }));
    useAgentFeedStore.getState().applyLive(ev({ seq: 2, phase: 'tool_call', tool_name: 'ws_grep' }));
    const rec = useAgentFeedStore.getState().records.a1;
    expect(rec.events.map((e) => e.seq)).toEqual([1, 2]);
    expect(rec.loading).toBe(false);
    expect(rec.sharedBrief).toEqual({ text: 'BRIEF\n', agents: 2 });
    // A second open of a loaded record does not read it again.
    await useAgentFeedStore.getState().open('a1');
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('holds live events that land during the read and merges them after it', async () => {
    const d = deferred<unknown>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(d.promise));
    const opening = useAgentFeedStore.getState().open('a1');
    useAgentFeedStore.getState().applyLive(ev({ seq: 2, phase: 'text', text: 'late' }));
    useAgentFeedStore.getState().applyLive(ev({ seq: 3, phase: 'stopped' }));
    d.resolve({
      ok: true,
      status: 200,
      json: async () => ({ meta: meta(), events: [ev({ seq: 1 }), ev({ seq: 2, phase: 'text', text: 'late' })], report: '' }),
    });
    await opening;
    const rec = useAgentFeedStore.getState().records.a1;
    expect(rec.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(rec.meta?.status).toBe('stopped');
    expect(rec.pending).toEqual([]);
    expect(rec.sharedBrief).toBeNull();
  });

  it('ignores runs nobody opened and says when there is no record', async () => {
    useAgentFeedStore.getState().applyLive(ev({ agent_id: 'ghost', seq: 1 }));
    expect(useAgentFeedStore.getState().records.ghost).toBeUndefined();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
    await useAgentFeedStore.getState().open('a9');
    expect(useAgentFeedStore.getState().records.a9.error).toBe('No record for this agent.');
    useAgentFeedStore.getState().forget('a9');
    expect(useAgentFeedStore.getState().records.a9).toBeUndefined();
  });
});
