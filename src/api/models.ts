import { del, get, post } from '@/api/client';

/** Model download manager (/api/models/*) — Settings > Models tab. */

export type ManagedModelGroup = 'transcription' | 'indexing' | 'local-chat';
export type ManagedModelState = 'absent' | 'queued' | 'downloading' | 'installed' | 'error';

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
