/** Typed client for the scheduled-tasks (cron) API. */
import { get, post, put, del, patch } from './client';

export type CronScheduleType = 'interval' | 'cron' | 'at';

export interface CronSchedule {
  type: CronScheduleType;
  /** interval */
  seconds?: number;
  every_minutes?: number;
  /** cron — hour/minute may be a comma string ("9,16") for multiple times */
  hour?: number | string;
  minute?: number | string;
  day_of_week?: string;
  /** at */
  run_at?: string;
  tz?: string;
}

export interface CronJob {
  id: string;
  name: string;
  prompt: string;
  schedule: CronSchedule;
  schedule_label: string;
  enabled: boolean;
  orphaned?: boolean;
  run_count?: number;
  last_run?: string | null;
  next_run?: string | null;
  run_state?: 'ok' | 'failed' | 'running' | 'stopped' | null;
  /** Optional per-job model override (Anthropic chat_models key). */
  model?: string;
  session_id?: string;
  session_exists?: boolean;
  session_title?: string | null;
}

export interface CronListResponse {
  jobs: CronJob[];
  scheduler_active: boolean;
  system_timezone: string;
}

export interface CronRun {
  run_id: string;
  job_id: string;
  job_name: string;
  session_id?: string;
  status: 'ok' | 'failed' | 'running' | string;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  text?: string | null;
  next_run?: string | null;
}

/** Metadata-only recent run (from GET /runs/recent) — drives unread badges. */
export interface CronRecentRun {
  run_id: string;
  job_id: string;
  job_name: string;
  session_id?: string;
  status: string;
  started_at: string;
}

export interface SchedulePreview {
  label?: string;
  next_run?: string | null;
  schedule?: CronSchedule;
  error?: string;
}

export interface CronCreateBody {
  name: string;
  prompt: string;
  schedule: CronSchedule;
  session_id: string;
  model?: string;
}

export interface CronUpdateBody {
  name?: string;
  prompt?: string;
  schedule?: CronSchedule;
  enabled?: boolean;
  session_id?: string;
  model?: string;
}

export const fetchCronJobs = () => get<CronListResponse>('/api/cron');

export const createCronJob = (body: CronCreateBody) =>
  post<{ created: boolean; job: CronJob }>('/api/cron', body);

export const updateCronJob = (id: string, body: CronUpdateBody) =>
  put<{ updated: boolean; job: CronJob }>(`/api/cron/${encodeURIComponent(id)}`, body);

export const stopCronJob = (id: string) =>
  post<{ stopping: boolean }>(`/api/cron/${encodeURIComponent(id)}/stop`, {});

export const deleteCronJob = (id: string) =>
  del<{ deleted: number }>(`/api/cron/${encodeURIComponent(id)}`);

export const toggleCronJob = (id: string) =>
  patch<{ job_id: string; enabled: boolean; next_run: string | null }>(
    `/api/cron/${encodeURIComponent(id)}/toggle`,
  );

export const runCronJob = (id: string) =>
  post<{ started?: boolean; error?: string }>(`/api/cron/${encodeURIComponent(id)}/run`);

export const previewSchedule = (schedule: CronSchedule) =>
  post<SchedulePreview>('/api/cron/preview', { schedule });

export const fetchCronHistory = (id: string, limit = 50) =>
  get<{ runs: CronRun[] }>(`/api/cron/${encodeURIComponent(id)}/history?limit=${limit}`);

export const fetchRecentRuns = (limit = 200) =>
  get<{ runs: CronRecentRun[] }>(`/api/cron/runs/recent?limit=${limit}`);
