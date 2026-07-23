/** REST client for the workflow runtime (WS-D). */
import { get, post, del } from '@/api/client';

export interface WorkflowRun {
  run_id: string;
  name: string;
  status: 'running' | 'done' | 'failed' | 'stopped' | 'stale';
  agents_spawned: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  cap_reached: boolean;
  error: string;
  phases?: unknown;
  result?: unknown;
  live?: boolean;
  started_at?: string;
  finished_at?: string | null;
  journal?: Array<Record<string, unknown>>;
}

export interface SavedWorkflow {
  name: string;
  description: string;
  phases: unknown[];
  trusted: boolean;
}

export const listRuns = (sessionId?: string) =>
  get<{ runs: WorkflowRun[] }>(`/api/workflows/runs${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`);

export const getRun = (runId: string) => get<WorkflowRun>(`/api/workflows/runs/${encodeURIComponent(runId)}`);

export const launchRun = (body: { script?: string; name?: string; session_id: string; args?: unknown; budget_usd?: number | null; model_id?: string }) =>
  post<{ run_id: string; status: string }>('/api/workflows/runs', body);

export const stopRun = (runId: string) => post<{ stopped: boolean }>(`/api/workflows/runs/${encodeURIComponent(runId)}/stop`, {});

export const listSaved = () => get<{ saved: SavedWorkflow[] }>('/api/workflows/saved');
export const approveSaved = (name: string) => post<{ approved: boolean }>(`/api/workflows/saved/${encodeURIComponent(name)}/approve`, {});
export const deleteSaved = (name: string) => del<{ deleted: boolean }>(`/api/workflows/saved/${encodeURIComponent(name)}`);

/** Subscribe to a run's live SSE events. Returns an unsubscribe fn.
 *  On a transient drop we let EventSource auto-reconnect (it resumes with a
 *  fresh snapshot) rather than force-closing, so one network blip no longer
 *  freezes the run card. The caller owns teardown: WorkflowRunCard closes the
 *  stream once the run is terminal (and on unmount) via the returned fn. */
export function subscribeRun(runId: string, onEvent: (ev: Record<string, unknown>) => void): () => void {
  const es = new EventSource(`/api/workflows/runs/${encodeURIComponent(runId)}/events`);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* ignore keepalives / malformed */
    }
  };
  // Default EventSource behavior reconnects on error unless we close(); leave it
  // to reconnect. Terminal teardown happens in the caller's effect cleanup.
  return () => es.close();
}
