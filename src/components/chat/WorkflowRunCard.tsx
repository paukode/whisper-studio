/**
 * Inline card for a launched/completed workflow run (WS-D). Subscribes to the
 * run's SSE stream for live phase/agent/cost plus each sub-agent's live event
 * log (text / tool_call / tool_result), rendered as expandable per-agent rows
 * via the shared AgentRow. Offers Stop while running and Refresh when terminal.
 */
import React, { useEffect } from 'react';
import { getRun, stopRun, subscribeRun } from '@/api/workflows';
import { useSessionStore } from '@/stores/sessionStore';
import { useWorkflowStore } from '@/stores/workflowStore';
import { AgentRow } from '@/components/chat/TeamReportCard';

const STATUS_COLOR: Record<string, string> = {
  running: 'var(--accent-warn, #b8860b)',
  done: 'var(--accent-ok, #2e7d32)',
  failed: 'var(--accent-record)',
  stopped: 'var(--text-secondary, gray)',
  stale: 'var(--text-secondary, gray)',
};

export const WorkflowRunCard: React.FC<{ runId: string; name?: string }> = ({ runId, name }) => {
  const sessionId = useSessionStore((s) => s.currentSessionId);
  const run = useWorkflowStore((s) => s.runs[runId]);
  const act = useWorkflowStore((s) => s.activity[runId]);

  const status = run?.status ?? act?.status ?? 'running';
  const terminal = status === 'done' || status === 'failed' || status === 'stopped' || status === 'stale';

  useEffect(() => {
    // The SSE stream sends an authoritative snapshot as its first frame, then
    // live events; that is the single source of truth (a getRun here would race
    // the snapshot and could clobber a terminal status with a stale 'running').
    // Close the stream once the run is terminal so a finished run doesn't leak
    // an EventSource (and its server-side generator) for the page's lifetime.
    if (terminal) return;
    const off = subscribeRun(runId, (ev) => useWorkflowStore.getState().applyEvent(runId, ev));
    return () => off();
  }, [runId, terminal]);

  // Ordered per-agent reports (built in render, not in a selector — the store
  // exposes stable refs; assembling the list here keeps the zustand v5 rule).
  const orderedAgents = (act?.agentOrder ?? [])
    .map((k) => act?.agentReports[k])
    .filter((a): a is NonNullable<typeof a> => Boolean(a));

  // Live activity fills in as SSE events arrive; the snapshot run row starts at
  // 0, so prefer whichever count is larger, and include the agents actually seen
  // on the detail stream.
  const agents = Math.max(run?.agents_spawned ?? 0, act?.agents ?? 0, orderedAgents.length);
  const cost = Math.max(run?.cost_usd ?? 0, act?.cost_usd ?? 0);
  const phase = act?.phase ?? '';

  const resume = async () => {
    if (!sessionId) return;
    // Resume is offered in the Workflows panel (which posts a resume run); here
    // we just refetch the snapshot to reflect the final state.
    void getRun(runId).then((r) => useWorkflowStore.getState().upsertRun(r)).catch(() => {});
  };

  return (
    <div className="workflow-card workflow-run">
      <div className="workflow-card-head">
        <span className="workflow-card-icon" aria-hidden="true">⚙️</span>
        <span className="workflow-card-title">{run?.name || name || 'Workflow'}</span>
        <span className="workflow-card-badge" style={{ color: STATUS_COLOR[status] ?? 'inherit' }}>{status}</span>
      </div>
      <div className="workflow-card-meta">
        {phase && <span>phase: {phase} · </span>}
        <span>{agents} agent{agents === 1 ? '' : 's'}</span>
        {cost > 0 && <span> · ${cost.toFixed(2)}</span>}
        {run?.cap_reached && <span> · cap reached</span>}
      </div>
      {orderedAgents.length > 0 && (
        <div className="workflow-card-agents">
          {orderedAgents.map((agent) => (
            <AgentRow key={agent.agent_id ?? agent.name} agent={agent} />
          ))}
        </div>
      )}
      {run?.error && <div className="workflow-card-meta workflow-err">{run.error}</div>}
      <div className="workflow-card-actions">
        {status === 'running' && (
          <button type="button" className="btn btn-sm" onClick={() => void stopRun(runId)}>Stop</button>
        )}
        {(status === 'failed' || status === 'stopped' || status === 'stale') && (
          <button type="button" className="btn btn-sm" onClick={() => void resume()}>Refresh</button>
        )}
      </div>
    </div>
  );
};
