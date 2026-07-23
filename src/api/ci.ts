/** REST client for CI watch + autofix (WS-J). */
import { get, post } from '@/api/client';

export interface CIJob {
  name?: string;
  status?: string;
  conclusion?: string;
  url?: string;
}

export interface CIRun {
  run_id?: number | null;
  status?: string | null;
  conclusion?: string | null;
  workflow?: string | null;
  url?: string | null;
  failing?: boolean;
  jobs?: CIJob[];
  failed_jobs?: string[];
}

export interface CIStatus {
  available: boolean;
  branch: string;
  run?: CIRun | null;
}

export interface CIAutofixPlan {
  run_id?: number | null;
  branch: string;
  url?: string | null;
  failed_jobs: string[];
  findings: Array<Record<string, unknown>>;
  script: string | null;
  summary: string;
  budget_usd?: number | null;
}

export const ciStatus = (branch?: string) =>
  get<CIStatus>(`/api/ci/status${branch ? `?branch=${encodeURIComponent(branch)}` : ''}`);

export const startWatch = (body: { branch?: string; session_id?: string }) =>
  post<{ task_id: string; branch: string; status: string }>('/api/ci/watch', body);

export interface CIWatchState {
  task_id: string;
  status: string; // task status: running | completed | failed | stopped | interrupted
  terminal: boolean;
  branch: string;
  run: (CIRun & { failed_jobs?: string[]; timed_out?: boolean; cancelled?: boolean }) | null;
}

/** Re-attach to the EXACT run a watch followed (survives reload), by task id. */
export const getWatch = (taskId: string) =>
  get<CIWatchState>(`/api/ci/watch/${encodeURIComponent(taskId)}`);

export const stopWatch = (taskId: string) =>
  post<{ stopped: boolean }>(`/api/ci/watch/${encodeURIComponent(taskId)}/stop`, {});

export const planAutofix = (body: { branch?: string; session_id?: string }) =>
  post<CIAutofixPlan>('/api/ci/autofix', body);
