import { del, get, post } from '@/api/client';

/** Model download manager (/api/models/*) — Settings > Models tab. */

export type ManagedModelGroup = 'transcription' | 'indexing' | 'local-chat';
export type ManagedModelState = 'absent' | 'queued' | 'downloading' | 'installed' | 'error';

/** The tool-call check a local chat model gets after its download
 *  (server/models_manager/tool_check.py): `running` while it loads the model
 *  and replays a first chat turn `total` times, then `passed` (every run
 *  called a tool), `failed` (stopped at the first run that did not, with
 *  `reason`), or `error` (the check could not run). */
export interface ToolCheck {
  state: 'running' | 'passed' | 'failed' | 'error';
  total: number;
  passed: number;
  /** running: the run in progress (0 while the model loads). */
  run?: number;
  /** passed/failed/error: how many runs it made. */
  runs?: number;
  reason?: string;
  checked_at?: string;
}

export interface ManagedModel {
  key: string;
  label: string;
  group: ManagedModelGroup;
  repo_id: string;
  /** One-line "what is this good for" from the catalog. */
  note?: string;
  installed: boolean;
  size_bytes_estimate: number | null;
  bytes_on_disk: number;
  /** Server-computed download fraction (0..0.99 in flight, 1 when installed,
   *  null with no size estimate). THE number every surface shows — never
   *  recompute it client-side from bytes/estimate. */
  progress: number | null;
  state: ManagedModelState;
  /** 1-based position in the download queue while state === 'queued'. */
  queue_position: number | null;
  error: string | null;
  /** Always-installed infrastructure model; hidden from Settings lists. */
  bundled?: boolean;
  /** Local chat models only; null when no check ran. */
  tool_check?: ToolCheck | null;
}

export interface ManagedModelStatus {
  state: ManagedModelState;
  bytes_on_disk: number;
  size_bytes_estimate: number | null;
  /** See ManagedModel.progress — the shared server-computed fraction. */
  progress: number | null;
  /** See ManagedModel.queue_position. */
  queue_position: number | null;
  error: string | null;
  /** See ManagedModel.tool_check. */
  tool_check?: ToolCheck | null;
}

export const fetchModelsCatalog = () =>
  get<{ models: ManagedModel[] }>('/api/models/catalog');

export const fetchModelsStatus = () =>
  get<{ models: Record<string, ManagedModelStatus> }>('/api/models/status');

export interface StartDownloadResult {
  started: boolean;
  /** True when both download slots were busy and the model joined the queue
   *  (HTTP 200 — queueing is not an error). */
  queued: boolean;
  key: string;
  state: 'downloading' | 'queued';
  queue_position: number | null;
}

export const startModelDownload = (key: string) =>
  post<StartDownloadResult>(`/api/models/${encodeURIComponent(key)}/download`);

export const cancelModelDownload = (key: string) =>
  post<{ cancelled: boolean; dequeued?: boolean }>(
    `/api/models/${encodeURIComponent(key)}/cancel`,
  );

export const deleteManagedModel = (key: string) =>
  del<{ deleted: boolean; stopped_llama_server?: boolean; message?: string }>(
    `/api/models/${encodeURIComponent(key)}`,
  );
