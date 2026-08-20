import { get, post, put, del, patch } from './client';
import type { Session } from '@/types/session';
import {
  SessionSearchResponseSchema,
  type SessionSearchResponse,
} from '@/types/schemas/session.schema';

export function getSessions(): Promise<Session[]> {
  return get<Session[]>('/api/sessions');
}

/** Content search behind the sidebar's "Search message content" toggle. */
export function searchSessions(q: string): Promise<SessionSearchResponse> {
  return get<SessionSearchResponse>(
    `/api/sessions/search?q=${encodeURIComponent(q)}`,
    { schema: SessionSearchResponseSchema },
  );
}

export function getSession(id: string): Promise<Session> {
  return get<Session>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function createSession(session: Partial<Session>): Promise<{ ok: boolean }> {
  return put<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(session.id ?? '')}`, session);
}

export function updateSession(id: string, data: Partial<Session>): Promise<{ ok: boolean }> {
  return put<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, data);
}

export function deleteSession(id: string): Promise<void> {
  return del<void>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function bulkDeleteSessions(ids: string[]): Promise<{ ok: boolean; deleted: number }> {
  return post<{ ok: boolean; deleted: number }>('/api/sessions/bulk-delete', { ids });
}

export interface SessionFlags {
  pinned?: boolean;
  archived?: boolean;
}

export function setSessionFlags(id: string, flags: SessionFlags): Promise<{ ok: boolean }> {
  return patch<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}/flags`, flags);
}

export function branchSession(id: string): Promise<{ new_session_id: string; name: string }> {
  return post<{ new_session_id: string; name: string }>(
    `/api/sessions/${encodeURIComponent(id)}/branch`, {},
  );
}

/** URL for GET /api/sessions/{id}/export — a portable JSONL backup of the
 *  session's chat history, transcript, and speaker names. Used with
 *  downloadUrl() rather than fetched inline; the server sets its own
 *  Content-Disposition filename. */
export function exportSessionUrl(id: string): string {
  return `/api/sessions/${encodeURIComponent(id)}/export`;
}

/** Reads a JSON error body ({error} or {detail}) from a failed response,
 *  falling back to the status text. Mirrors ImportSkillsDialog's readError. */
async function readImportError(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { error?: string; detail?: string };
    return body.error || body.detail || `HTTP ${resp.status}`;
  } catch {
    return `HTTP ${resp.status}`;
  }
}

/** Create a new session from a file exported by GET .../export. Multipart
 *  upload via raw fetch, not the shared post() helper — that always
 *  JSON-encodes its body, which can't carry a File. */
export function importSession(file: File): Promise<{ new_session_id: string; title: string }> {
  const fd = new FormData();
  fd.append('file', file);
  return fetch('/api/sessions/import-transcript', { method: 'POST', body: fd }).then(async (resp) => {
    if (!resp.ok) throw new Error(await readImportError(resp));
    return resp.json() as Promise<{ new_session_id: string; title: string }>;
  });
}

export interface BulkImportResult {
  imported: { new_session_id: string; title: string; filename: string }[];
  failed: { filename: string; error: string }[];
}

/** Create many new sessions from multiple files in one request — each file
 *  is either a plain .jsonl export or a .zip bundle (as produced by
 *  bulkExportSessions) whose .jsonl entries are imported individually. */
export function importSessions(files: File[]): Promise<BulkImportResult> {
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  return fetch('/api/sessions/bulk-import', { method: 'POST', body: fd }).then(async (resp) => {
    if (!resp.ok) throw new Error(await readImportError(resp));
    return resp.json() as Promise<BulkImportResult>;
  });
}

/** Zip many sessions' portable JSONL exports into one download (sidebar
 *  multi-select 'Export'). POST (not a plain URL like exportSessionUrl)
 *  since the id list needs to go in a body; fetched as a blob and handed to
 *  downloadFile() rather than downloadUrl(), mirroring the other export
 *  formats that build content client-side. */
export async function bulkExportSessions(ids: string[]): Promise<{ blob: Blob; filename: string }> {
  const resp = await fetch('/api/sessions/bulk-export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  if (!resp.ok) throw new Error(await readImportError(resp));
  const disposition = resp.headers.get('Content-Disposition') || '';
  const match = /filename="([^"]+)"/.exec(disposition);
  const filename = match?.[1] || `sessions-export-${new Date().toISOString().slice(0, 10)}.zip`;
  return { blob: await resp.blob(), filename };
}

/** One passage behind a grounded answer, with its retrieval provenance:
 *  how it got into the prompt — an exact-term match ('keyword'), a semantic
 *  match ('semantic'), an entity named in the question ('entity'), or the
 *  graph hop off the matches ('related'). Entity/related passages carry the
 *  linking entity names for chips that deep-link into the index explorer. */
export interface AnswerSource {
  /** Workspace-relative path (display). */
  path: string;
  /** Absolute path (open/link target). */
  abs: string;
  /** The indexed workspace root — the index-explorer deep-link scope. */
  ws?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  kind: 'keyword' | 'semantic' | 'entity' | 'related';
  entities: string[];
  snippet: string;
}

/** The passages behind one grounded answer, keyed by the grounding id the
 *  `grounding` SSE event carried. 404s (surfaced as a thrown ApiError) when
 *  the row was trimmed or the database cleared. */
export function getAnswerSources(
  sessionId: string,
  groundingId: string,
): Promise<{ id: string; sources: AnswerSource[] }> {
  return get<{ id: string; sources: AnswerSource[] }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/grounding/${encodeURIComponent(groundingId)}`,
  );
}

export type WorkspaceApp = 'vscode' | 'kiro' | 'finder';

export function openSessionWorkspace(id: string, app: WorkspaceApp): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(
    `/api/sessions/${encodeURIComponent(id)}/open-workspace`, { app },
  );
}
