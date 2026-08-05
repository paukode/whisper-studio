import { del, get, post } from '@/api/client';

/** In-app model browser (/api/models/browse/*) — Settings > Models > Discover.
 *  Search Hugging Face for GGUF chat models, pick a quant, install it as a
 *  config-driven local model that then appears in the Models list and the
 *  composer picker. */

export interface BrowseResult {
  repo_id: string;
  author: string;
  name: string;
  label: string;
  downloads: number | null;
  likes: number | null;
  trending_score: number | null;
  arch: string | null;
  /** Human parameter-size label parsed from the repo name (e.g. "0.6B", "8B"). */
  param_size: string | null;
  context_length: number | null;
  gated: boolean;
  /** Always true in returned rows — the backend hides unsupported archs. */
  supported: boolean;
}

export interface BrowseQuant {
  quant: string;
  /** First shard for a sharded model, else the single file — the load target. */
  filename: string;
  size_bytes: number;
  is_sharded: boolean;
  shard_count: number;
  recommended: boolean;
}

export interface BrowseRepoDetail {
  repo_id: string;
  arch: string | null;
  supported: boolean;
  context_length: number | null;
  has_chat_template: boolean;
  gated: boolean;
  recommended_filename: string | null;
  quants: BrowseQuant[];
}

export interface InstallResult {
  key: string;
  id: string;
  label: string;
  ctx: number;
  supports_thinking: boolean;
  supports_tools: boolean;
  /** models-manager start result: "started" | "queued" | "already_installed" | … */
  download: string;
}

export type BrowseSort = 'trending' | 'downloads';

export const searchModels = (params: {
  q?: string;
  author?: string;
  sort?: BrowseSort;
  all?: boolean;
}) => {
  const qs = new URLSearchParams();
  if (params.q) qs.set('q', params.q);
  if (params.author) qs.set('author', params.author);
  if (params.sort) qs.set('sort', params.sort);
  if (params.all) qs.set('all', '1');
  return get<{ results: BrowseResult[]; count: number }>(
    `/api/models/browse/search?${qs.toString()}`,
  );
};

export const fetchRepoDetail = (repoId: string) =>
  get<BrowseRepoDetail>(`/api/models/browse/repo/${repoId}`);

export const installModel = (body: {
  repo_id: string;
  filename: string;
  label?: string;
  n_ctx?: number;
}) => post<InstallResult>('/api/models/browse/install', body);

export const uninstallBrowsedModel = (key: string) =>
  del<{ key: string; removed: boolean; stopped_llama_server: boolean; message?: string }>(
    `/api/models/browse/${encodeURIComponent(key)}`,
  );
