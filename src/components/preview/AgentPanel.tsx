import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ChevronIcon } from '@/components/common/icons';
import { useNow } from '@/hooks/useNow';
import { useUIStore } from '@/stores/uiStore';
import {
  formatSeconds,
  recordReadout,
  useAgentFeedStore,
  type AgentProgressEvent,
} from '@/stores/agentFeedStore';

const EXTEND_ROUNDS = 20;

/**
 * AgentPanel: one agent's full feed in the dock, live while it runs. The
 * status line (state, model, rounds against the cap, time, cost, Extend),
 * then everything the agent did in order: what it wrote, every tool call
 * with its result folded under it, and what it is waiting on. The dock
 * header carries the name, Stop (AgentStopButton) and close.
 */
export const AgentPanel: React.FC<{ agentId: string }> = ({ agentId }) => {
  const rec = useAgentFeedStore((s) => s.records[agentId]);
  const open = useAgentFeedStore((s) => s.open);
  const forget = useAgentFeedStore((s) => s.forget);
  useEffect(() => {
    void open(agentId);
    return () => forget(agentId);
  }, [agentId, open, forget]);

  const running = rec?.meta?.status === 'running';
  const now = useNow(1000, running);
  const feedRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const eventCount = rec?.events.length ?? 0;
  // Follow the tail while the user is reading the end of it.
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [eventCount]);
  const onScroll = () => {
    const el = feedRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  if (!rec || rec.loading) return <div className="agent-pane agent-pane-note">Loading the record…</div>;
  if (rec.error) return <div className="agent-pane agent-pane-note">{rec.error}</div>;

  const readout = recordReadout(rec, now);
  const items = feedItems(rec.events);
  const waitingOn = running ? waitingFor(rec.events) : null;
  return (
    <div className="agent-pane">
      <div className="agent-pane-status">
        <span>
          <span className={`agent-pane-dot${readout.running ? '' : ' done'}`} aria-hidden="true" />
          {readout.status}
        </span>
        {readout.model && <span>{readout.model}</span>}
        <span>
          round {readout.rounds}
          {readout.roundsCap !== null ? ` of ${readout.roundsCap}` : ''}
        </span>
        {readout.elapsedS !== null && <span>{formatSeconds(readout.elapsedS)}</span>}
        {readout.costUsd !== null && <span>${readout.costUsd.toFixed(2)}</span>}
        {readout.running && <ExtendButton agentId={agentId} />}
      </div>
      <div className="agent-pane-feed" ref={feedRef} onScroll={onScroll}>
        {rec.meta?.task && (
          <div className="agent-ev">
            <span className="agent-ev-label">task</span>
            <div className="agent-ev-body agent-ev-task">{rec.meta.task}</div>
          </div>
        )}
        {items.map((item, i) => (
          <FeedRow key={i} item={item} />
        ))}
        {waitingOn !== null && (
          <div className="agent-ev agent-ev-wait">
            <span className="agent-ev-label" />
            <div className="agent-ev-body">
              <span className="trace-spinner" aria-hidden="true" /> {waitingOn ? `waiting for ${waitingOn}` : 'thinking…'}
            </div>
          </div>
        )}
        {!running && rec.report && (
          <details className="agent-pane-report" open>
            <summary>Report</summary>
            <MarkdownRenderer content={rec.report} />
          </details>
        )}
      </div>
    </div>
  );
};

/** Stop in the dock header, shown while the run is live. */
export const AgentStopButton: React.FC<{ agentId: string }> = ({ agentId }) => {
  const running = useAgentFeedStore((s) => s.records[agentId]?.meta?.status === 'running');
  const [busy, setBusy] = useState(false);
  const stop = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/background-tasks/${encodeURIComponent(agentId)}/stop`, { method: 'POST' });
      const data = (await r.json().catch(() => ({}))) as { stopped?: boolean };
      if (!r.ok || !data.stopped) {
        useUIStore.getState().addToast({ type: 'warning', message: 'This agent could not be stopped; it may have just finished.', duration: 4000 });
      }
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Stop did not reach the server.', duration: 4000 });
    } finally {
      setBusy(false);
    }
  }, [agentId]);
  if (!running) return null;
  return (
    <button type="button" className="agent-pane-stop" onClick={() => void stop()} disabled={busy} title="Stop this agent">
      Stop
    </button>
  );
};

const ExtendButton: React.FC<{ agentId: string }> = ({ agentId }) => {
  const [busy, setBusy] = useState(false);
  const extend = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/extend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rounds: EXTEND_ROUNDS }),
      });
      const data = (await r.json().catch(() => ({}))) as { error?: string; extension?: { rounds?: number } };
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      useUIStore.getState().addToast({
        type: 'info',
        message: `${EXTEND_ROUNDS} more rounds granted (${data.extension?.rounds ?? EXTEND_ROUNDS} extra in total).`,
        duration: 4000,
      });
    } catch (e) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Could not extend the agent: ${e instanceof Error ? e.message : String(e)}`,
        duration: 5000,
      });
    } finally {
      setBusy(false);
    }
  }, [agentId]);
  return (
    <button type="button" className="agent-pane-extend" onClick={() => void extend()} disabled={busy} title="Grant more rounds while the context is still loaded">
      {busy ? 'Extending…' : `Extend +${EXTEND_ROUNDS}`}
    </button>
  );
};

type FeedItem =
  | { kind: 'text'; ev: AgentProgressEvent }
  | { kind: 'tool'; call: AgentProgressEvent | null; result: AgentProgressEvent | null }
  | { kind: 'end'; ev: AgentProgressEvent };

/** The feed in reading order: the agent's words, each tool call with its
 *  result folded under it, and how the run ended. Rounds and the start
 *  notice feed the status line instead. */
export function feedItems(events: AgentProgressEvent[]): FeedItem[] {
  const items: FeedItem[] = [];
  const open: Array<FeedItem & { kind: 'tool' }> = [];
  for (const ev of events) {
    switch (ev.phase) {
      case 'text':
        if (ev.text?.trim()) items.push({ kind: 'text', ev });
        break;
      case 'tool_call': {
        const item = { kind: 'tool' as const, call: ev, result: null };
        items.push(item);
        open.push(item);
        break;
      }
      case 'tool_result': {
        const i = open.findIndex((c) => c.call?.tool_name === ev.tool_name);
        if (i >= 0) open.splice(i, 1)[0].result = ev;
        else items.push({ kind: 'tool', call: null, result: ev });
        break;
      }
      case 'completed':
      case 'turn_limit':
      case 'failed':
      case 'stopped':
      case 'finished':
        items.push({ kind: 'end', ev });
        break;
      default:
        break;
    }
  }
  return items;
}

/** The tool a running agent is waiting on, '' while it is thinking. */
export function waitingFor(events: AgentProgressEvent[]): string {
  const pending: string[] = [];
  for (const ev of events) {
    if (ev.phase === 'tool_call') pending.push(ev.tool_name ?? '');
    else if (ev.phase === 'tool_result') {
      const i = pending.indexOf(ev.tool_name ?? '');
      if (i >= 0) pending.splice(i, 1);
    }
  }
  return pending[0] ?? '';
}

const END_TEXT: Record<string, string> = {
  completed: 'done',
  turn_limit: 'stopped at the turn limit',
  failed: 'failed',
  stopped: 'stopped',
};

const FeedRow: React.FC<{ item: FeedItem }> = ({ item }) => {
  if (item.kind === 'text') {
    return (
      <div className="agent-ev">
        <span className="agent-ev-label">model</span>
        <div className="agent-ev-body">
          <MarkdownRenderer content={item.ev.text ?? ''} stepFormat />
        </div>
      </div>
    );
  }
  if (item.kind === 'end') {
    const ev = item.ev;
    const words = ev.phase === 'finished' ? (ev.stop_reason === 'completed' ? 'done' : ev.stop_reason ?? ev.status ?? 'done') : END_TEXT[ev.phase];
    const rounds = ev.turns_used ? ` · ${ev.turns_used} round${ev.turns_used === 1 ? '' : 's'}` : '';
    return (
      <div className="agent-ev agent-ev-end">
        <span className="agent-ev-label">run</span>
        <div className="agent-ev-body">
          {words}
          {rounds}
          {ev.error ? `: ${ev.error}` : ''}
        </div>
      </div>
    );
  }
  const { call, result } = item;
  const name = call?.tool_name ?? result?.tool_name ?? '';
  const input = call?.tool_input_full && call.tool_input_full.length > (call.tool_input_preview?.length ?? 0) ? call.tool_input_full : null;
  const output = result ? result.output_full || result.output_preview || '' : '';
  return (
    <div className={`agent-ev agent-ev-tool${result?.status === 'error' ? ' errored' : ''}`}>
      <span className="agent-ev-label">tool</span>
      <div className="agent-ev-body">
        <span className="mono">{name}</span>
        {call?.tool_input_preview && <span className="mono agent-ev-arg"> {call.tool_input_preview}</span>}
        {(result || input) && (
          <details className="agent-ev-result">
            <summary>
              <ChevronIcon direction="right" />
              {result ? `result · ${output.length} chars${result.status === 'error' ? ' · error' : ''}` : 'input'}
            </summary>
            {input && <pre className="agent-ev-pre">{input}</pre>}
            {result && <pre className="agent-ev-pre">{output}</pre>}
          </details>
        )}
      </div>
    </div>
  );
};
