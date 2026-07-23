import { get, post, put } from './client';
import type { FileTreeEntry } from '@/types/workspace';

interface ListDirResponse {
  entries: FileTreeEntry[];
}

/** Response from GET /api/workspace/file (non-raw) for binary files */
export interface BinaryFileInfo {
  path: string;
  binary: true;
  type: 'image' | 'pdf' | 'spreadsheet' | 'word' | 'presentation' | 'binary';
  size: number;
}

/** Response from GET /api/workspace/file (non-raw) for text files */
interface TextFileInfo {
  path: string;
  content: string;
  size: number;
}

export async function listDir(path: string): Promise<FileTreeEntry[]> {
  const data = await get<ListDirResponse | FileTreeEntry[]>(`/api/workspace/list-dir?path=${encodeURIComponent(path)}`);
  // Backend returns { entries: [...] } — unwrap it
  if (data && !Array.isArray(data) && Array.isArray((data as ListDirResponse).entries)) {
    return (data as ListDirResponse).entries;
  }
  // Fallback: if it's already an array, use it directly
  if (Array.isArray(data)) return data;
  return [];
}

/** Read a text file's content as a raw string. Bypasses the shared client on
 *  purpose: the client sniffs the content-type and would `response.json()`-parse
 *  a `.json` file into an object, breaking the `Promise<string>` contract. Reading
 *  the body with `response.text()` guarantees a string regardless of content-type. */
export async function readFile(path: string): Promise<string> {
  const response = await fetch(`/api/workspace/file?path=${encodeURIComponent(path)}&raw=true`);
  if (!response.ok) {
    throw new Error(`Failed to read file: ${response.status} ${response.statusText}`);
  }
  return response.text();
}

/** Query file metadata without fetching content. Binary files return {binary:true, type}. */
export function queryFile(path: string): Promise<TextFileInfo | BinaryFileInfo> {
  return get<TextFileInfo | BinaryFileInfo>(`/api/workspace/file?path=${encodeURIComponent(path)}`);
}

/** Get the raw file URL for use in img/iframe/embed src attributes. */
export function rawFileUrl(path: string): string {
  return `/api/workspace/file?path=${encodeURIComponent(path)}&raw=true`;
}

/** Raw URL for a chat 'source' file. Unlike rawFileUrl (workspace-relative),
 *  this resolves the absolute paths grounded-index citations use against the
 *  indexed folders, so cited files outside the connected workspace stream too. */
export function sourceFileRawUrl(path: string): string {
  return `/api/workspace/source-file?path=${encodeURIComponent(path)}&raw=true`;
}

export function writeFile(path: string, content: string): Promise<void> {
  return post<void>('/api/workspace/write', { path, content });
}

export function deleteFile(path: string): Promise<void> {
  return post<void>('/api/workspace/delete', { path });
}

/** Rename a file/dir in place. `newName` is a basename only (the backend
 *  renames within the same directory and rejects path separators). */
export function renameFile(oldPath: string, newName: string): Promise<void> {
  return post<void>('/api/workspace/rename', { path: oldPath, new_name: newName });
}

// ── Workspace semantic index (GraphRAG) ──────────────────────────────────────

export interface IndexStatus {
  path?: string;
  indexed: boolean;
  building: boolean;
  progress?: { done: number; total: number; current: string | null } | null;
  error?: string | null;
  files?: number;
  chunks?: number;
  nodes?: number;
  last_indexed_at?: string | null;
}

/** Per-workspace index settings (each indexed folder has its own). */
export interface IndexSettings {
  schedule: {
    enabled: boolean;
    hour: number;
    frequency: 'daily' | 'every_n_days' | 'weekly';
    interval_days: number;
    weekday: 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';
  };
  typed_relations: { enabled: boolean; engine: 'none' | 'haiku' | 'local' | 'gliner2' };
  chunk_context: { mode: 'off' | 'filename' | 'llm'; engine: 'haiku' | 'local' };
  /** On-device NER model: gliner_large (default, best multilingual) or GLiNER2
   *  (English-strong). The entity label set (business vs code) is auto-picked
   *  per file on the backend. */
  ner_model: 'gliner' | 'gliner2';
  refresh_when_closed: boolean;
}
export type IndexSettingsPatch = {
  schedule?: Partial<IndexSettings['schedule']>;
  typed_relations?: Partial<IndexSettings['typed_relations']>;
  chunk_context?: Partial<IndexSettings['chunk_context']>;
  ner_model?: IndexSettings['ner_model'];
  refresh_when_closed?: boolean;
};

export function indexStatus(path: string): Promise<IndexStatus> {
  return get<IndexStatus>(`/api/workspace/index/status?path=${encodeURIComponent(path)}`);
}

export function buildIndex(path: string): Promise<{ started: boolean; reason?: string }> {
  return post('/api/workspace/index/build', { path });
}

export function removeIndex(path: string): Promise<{ removed: boolean }> {
  return post('/api/workspace/index/remove', { path });
}

export function cancelIndex(path: string): Promise<{ cancelling: boolean }> {
  return post('/api/workspace/index/cancel', { path });
}

export interface IndexInfo {
  path: string;
  name: string;
  files: number;
  chunks: number;
  last_indexed_at: string | null;
}

export function listIndexes(): Promise<{ indexes: IndexInfo[] }> {
  return get<{ indexes: IndexInfo[] }>('/api/workspace/index/list');
}

export interface IndexGraphNode { id: string; name: string; chunks?: number; workspace?: string; group?: number; type?: 'file' | 'entity'; label?: string; community?: number; degree?: number; ux?: number; uy?: number; description?: string; }
export interface IndexGraphEdge { source: string; target: string; weight?: number; weight_norm?: number; entities?: string[]; cross?: boolean; relation?: string; score?: number; }
export interface IndexGraphWorkspace { path: string; name: string; files: number; group: number; }
export interface IndexGraph {
  nodes: IndexGraphNode[];
  edges: IndexGraphEdge[];
  root: string;
  truncated?: boolean;
  workspaces?: IndexGraphWorkspace[];
}

export function getIndexGraph(path: string): Promise<IndexGraph> {
  return get<IndexGraph>(`/api/workspace/index/graph?path=${encodeURIComponent(path)}`);
}

/** Unified graph across all indexed workspaces (nodes grouped by workspace). */
export function getAllIndexesGraph(): Promise<IndexGraph> {
  return get<IndexGraph>('/api/workspace/index/graph/all');
}

/** Semantic-map layout: same file graph but with a 2D embedding projection
 *  (ux/uy per node) so files close in meaning sit together. Powers "UMAP map". */
export function getIndexUmapGraph(path: string): Promise<IndexGraph> {
  return get<IndexGraph>(`/api/workspace/index/graph/umap?path=${encodeURIComponent(path)}`);
}

/** Cross-workspace semantic map: a single UMAP over every indexed file, so
 *  "All indexed" + "UMAP map" spans all folders instead of one. */
export function getAllIndexesUmapGraph(): Promise<IndexGraph> {
  return get<IndexGraph>('/api/workspace/index/graph/umap/all');
}

/** Entity-centric graph: one entity at the centre linked to every file that
 *  mentions it ("everything about this person"). */
export function getIndexEntityGraph(path: string, name: string, label = ''): Promise<IndexGraph> {
  return get<IndexGraph>(
    `/api/workspace/index/graph/entity?path=${encodeURIComponent(path)}&name=${encodeURIComponent(name)}&label=${encodeURIComponent(label)}`,
  );
}

/** This folder's own index settings (schedule, typed relations, background). */
export function getIndexSettings(path: string): Promise<IndexSettings> {
  return get<IndexSettings>(`/api/workspace/index/settings?path=${encodeURIComponent(path)}`);
}

/** Update one folder's settings; the backend re-applies its scheduled job and
 *  syncs the background helper. Returns the full updated settings. */
export function updateIndexSettings(path: string, patch: IndexSettingsPatch): Promise<IndexSettings> {
  return put<IndexSettings>('/api/workspace/index/settings', { path, ...patch });
}

/** Whether the background refresh helper is installed + whether the platform
 *  supports it (macOS). The per-folder on/off lives in each folder's settings. */
export interface IndexAgentStatus { installed: boolean; supported: boolean; }

export function getIndexAgent(): Promise<IndexAgentStatus> {
  return get<IndexAgentStatus>('/api/workspace/index/agent');
}

/** Remove one folder from the recent list (no-op server-side if it's indexed). */
export function removeRecentWorkspace(path: string): Promise<{ recent: string[] }> {
  return post<{ recent: string[] }>('/api/workspace/recent/remove', { path });
}

/** Drop every not-indexed recent, keeping indexed folders. */
export function clearUnindexedRecents(): Promise<{ recent: string[] }> {
  return post<{ recent: string[] }>('/api/workspace/recent/clear-unindexed', {});
}

