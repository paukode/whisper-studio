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
  /** True when this (first) local install flipped the index LLM to follow the
   *  on-device model (fresh installs default index_llm to cloud Haiku). */
  adopted_index_llm?: boolean;
}

/** A curated on-device model the Discover tab recommends for one-click install.
 *  The app ships no local models by default, so this is the primary way in. */
export interface RecommendedModel {
  key: string;
  repo_id: string;
  author: string;
  name: string;
  label: string;
  filename: string;
  quant: string;
  param_size: string | null;
  ctx: number | null;
  supports_thinking: boolean;
  supports_tools: boolean;
  /** Already present on disk (from a prior install or release). */
  downloaded: boolean;
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

/** The curated Recommended catalog (Settings > Models > Discover). */
export const fetchRecommendedModels = () =>
  get<{ recommended: RecommendedModel[]; count: number }>('/api/models/browse/recommended');

/** One-click install a recommendation by its canonical key. */
export const installRecommendedModel = (key: string) =>
  post<InstallResult>('/api/models/browse/install-recommended', { key });

export interface RemoveFromListResult {
  key: string;
  /** "local" = a local model, deleted outright (weights + config), re-downloadable
   *  from Discover; "bedrock" = a cloud model, tombstoned into chat_models_disabled
   *  (recoverable via Chat model visibility). */
  scope: 'local' | 'bedrock';
  /** Present for a local removal — whether a config entry was deleted. */
  removed_entry?: boolean;
  /** Present for a Bedrock removal — whether it was newly hidden. */
  tombstoned?: boolean;
  weights_deleted: boolean;
  stopped_llama_server: boolean;
  message?: string;
}

/** Remove a chat model from the list + picker entirely: a local model is deleted
 *  outright (weights + config, re-downloadable from Discover); a Bedrock model is
 *  hidden via chat_models_disabled (recoverable via Chat model visibility). */
export const removeModelFromList = (key: string) =>
  del<RemoveFromListResult>(`/api/models/browse/${encodeURIComponent(key)}/entry`);
