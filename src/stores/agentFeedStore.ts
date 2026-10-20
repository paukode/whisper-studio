/**
 * The agent pane's data: one record per opened agent, the on-disk journal
 * read once (GET /api/agents/{id}/record) and then followed live through
 * agent_progress frames on the app's event channel (sessionRuntimes). Events
 * merge by the journal's seq, so the read and the live tail never duplicate
 * or lose one, whichever lands first. A live event without a seq (the
 * runtime's own stopped and failed notices) is not a journal line: it only
 * updates the run's status. A record exists only while something shows it;
 * the pane forgets it on close.
 */
import { create } from 'zustand';
import type { TeamProgressEvent } from '@/types/chat';

export interface AgentProgressEvent extends TeamProgressEvent {
  /** The journal line number; absent on notices that are not journal lines. */
  seq?: number;
  ts?: string;
}

/** meta.json of a run (server/agents/journal.py). */
export interface AgentRecordMeta {
  agent_id: string;
  session_id?: string;
  task?: string;
  agent_name?: string | null;
  agent_type?: string;
  model?: string;
  max_turns?: number | null;
  deadline_seconds?: number | null;
  status?: string;
  stop_reason?: string;
  started_at?: string | null;
  finished_at?: string | null;
  rounds?: number;
  usage?: { cost_usd?: number };
  team_id?: string | null;
  parent_agent_id?: string | null;
  workflow_run_id?: string | null;
}

/** The opening a workflow agent's task shares with other agents of the same
 *  run (server/agents/journal.py shared_brief): the text, and how many other
 *  agents' tasks start with it. */
export interface SharedBrief {
  text: string;
  agents: number;
}

export interface AgentRecord {
  meta: AgentRecordMeta | null;
  events: AgentProgressEvent[];
  report: string;
  sharedBrief: SharedBrief | null;
  loading: boolean;
  /** 404 or a network failure: the pane says so instead of staying blank. */
  error: string | null;
  /** Live events that arrived before the read finished, merged after it. */
  pending: AgentProgressEvent[];
}

interface AgentFeedState {
  records: Record<string, AgentRecord>;
  open: (agentId: string) => Promise<void>;
  applyLive: (ev: AgentProgressEvent) => void;
  forget: (agentId: string) => void;
}

function lastSeq(events: AgentProgressEvent[]): number {
  for (let i = events.length - 1; i >= 0; i--) {
    const seq = events[i].seq;
    if (typeof seq === 'number') return seq;
  }
  return 0;
}

/** The events numbered past what the record holds, appended in order. Returns
 *  the same array when nothing is new. */
export function mergeEvents(events: AgentProgressEvent[], incoming: AgentProgressEvent[]): AgentProgressEvent[] {
  let out = events;
  let last = lastSeq(events);
  for (const ev of incoming) {
    if (typeof ev.seq !== 'number' || ev.seq <= last) continue;
    if (out === events) out = [...events];
    out.push(ev);
    last = ev.seq;
  }
  return out;
}

/** What one event says about the run itself. */
export function metaAfter(meta: AgentRecordMeta | null, ev: AgentProgressEvent): AgentRecordMeta | null {
  if (!meta) return meta;
  switch (ev.phase) {
    case 'started':
      return { ...meta, status: 'running', model: ev.model ?? meta.model, max_turns: ev.max_turns ?? meta.max_turns };
    case 'turn_start':
      return {
        ...meta,
        status: 'running',
        rounds: Math.max(meta.rounds ?? 0, (ev.turn ?? 1) - 1),
        max_turns: ev.max_turns ?? meta.max_turns,
      };
    case 'completed':
      return { ...meta, status: 'completed', stop_reason: 'completed', rounds: ev.turns_used ?? meta.rounds };
    case 'turn_limit':
      return { ...meta, status: 'completed', stop_reason: ev.stop_reason ?? 'turn_limit', rounds: ev.turns_used ?? meta.rounds };
    case 'failed':
      return { ...meta, status: 'failed', stop_reason: 'error' };
    case 'stopped':
      return { ...meta, status: 'stopped', stop_reason: 'cancelled' };
    case 'finished':
      return {
        ...meta,
        status: ev.status ?? meta.status,
        stop_reason: ev.stop_reason ?? meta.stop_reason,
        rounds: ev.turns_used ?? meta.rounds,
      };
    default:
      return meta;
  }
}

const EMPTY: AgentRecord = { meta: null, events: [], report: '', sharedBrief: null, loading: true, error: null, pending: [] };

export const useAgentFeedStore = create<AgentFeedState>((set, get) => ({
  records: {},

  open: async (agentId) => {
    const existing = get().records[agentId];
    if (existing && !existing.error) return;
    set((s) => ({ records: { ...s.records, [agentId]: { ...EMPTY } } }));
    let loaded: Pick<AgentRecord, 'meta' | 'events' | 'report' | 'sharedBrief'> | null = null;
    let error: string | null = null;
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/record`);
      if (r.status === 404) error = 'No record for this agent.';
      else if (!r.ok) error = `The record could not be read (HTTP ${r.status}).`;
      else {
        const data = (await r.json()) as {
          meta: AgentRecordMeta;
          events: AgentProgressEvent[];
          report: string;
          shared_brief: SharedBrief | null;
        };
        loaded = { meta: data.meta, events: data.events, report: data.report, sharedBrief: data.shared_brief ?? null };
      }
    } catch {
      error = 'The record could not be read.';
    }
    set((s) => {
      const rec = s.records[agentId];
      if (!rec) return s; // closed while loading
      if (!loaded) return { records: { ...s.records, [agentId]: { ...rec, loading: false, error } } };
      let meta = loaded.meta;
      for (const ev of rec.pending) meta = metaAfter(meta, ev);
      const next: AgentRecord = {
        meta,
        events: mergeEvents(loaded.events, rec.pending),
        report: loaded.report,
        sharedBrief: loaded.sharedBrief,
        loading: false,
        error: null,
        pending: [],
      };
      return { records: { ...s.records, [agentId]: next } };
    });
  },

  applyLive: (ev) => {
    const agentId = ev.agent_id ?? '';
    set((s) => {
      const rec = s.records[agentId];
      if (!rec) return s;
      if (rec.loading) {
        return { records: { ...s.records, [agentId]: { ...rec, pending: [...rec.pending, ev] } } };
      }
      const events = mergeEvents(rec.events, [ev]);
      const meta = metaAfter(rec.meta, ev);
      if (events === rec.events && meta === rec.meta) return s;
      return { records: { ...s.records, [agentId]: { ...rec, events, meta } } };
    });
  },

  forget: (agentId) =>
    set((s) => {
      if (!(agentId in s.records)) return s;
      const records = { ...s.records };
      delete records[agentId];
      return { records };
    }),
}));

/** The status line of the pane, read off the record: the words for the run's
 *  state, the model, rounds spent against the cap, time and estimated cost. */
export interface RecordReadout {
  status: string;
  running: boolean;
  model: string;
  rounds: number;
  roundsCap: number | null;
  elapsedS: number | null;
  costUsd: number | null;
}

const STOP_REASON_TEXT: Record<string, string> = {
  completed: 'done',
  turn_limit: 'stopped at the turn limit',
  deadline: 'stopped at the time limit',
  cost_cap: 'stopped at the cost cap',
  refused_calls: 'stopped after repeated refused calls',
  cancelled: 'stopped',
  error: 'failed',
};

export function recordReadout(rec: AgentRecord, now: number): RecordReadout {
  const meta = rec.meta;
  let lastTurn: AgentProgressEvent | undefined;
  for (let i = rec.events.length - 1; i >= 0; i--) {
    if (rec.events[i].phase === 'turn_start') {
      lastTurn = rec.events[i];
      break;
    }
  }
  const running = meta?.status === 'running';
  let status: string;
  if (running) status = lastTurn?.budget_state === 'finishing' ? 'finishing up' : 'running';
  else if (meta?.status === 'failed') status = 'failed';
  else status = STOP_REASON_TEXT[meta?.stop_reason ?? ''] ?? (meta?.status === 'stopped' ? 'stopped' : 'done');
  const started = meta?.started_at ? Date.parse(meta.started_at) : NaN;
  const ended = meta?.finished_at ? Date.parse(meta.finished_at) : now;
  const elapsedS = Number.isFinite(started) && Number.isFinite(ended) ? Math.max(0, (ended - started) / 1000) : null;
  return {
    status,
    running,
    model: meta?.model ?? '',
    rounds: Math.max(meta?.rounds ?? 0, lastTurn?.turn !== undefined ? lastTurn.turn - 1 : 0),
    roundsCap: lastTurn?.max_turns ?? meta?.max_turns ?? null,
    elapsedS,
    costUsd: lastTurn?.cost_usd ?? meta?.usage?.cost_usd ?? null,
  };
}

/** "42s" / "7m 51s" / "8m". Rounds before it splits, so 119.6 s reads
 *  "2m", not "1m 60s". */
export function formatSeconds(s: number): string {
  const total = Math.round(s);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const rest = total % 60;
  return rest ? `${m}m ${String(rest).padStart(2, '0')}s` : `${m}m`;
}
