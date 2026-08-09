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

export type WorkspaceApp = 'vscode' | 'kiro' | 'finder';

export function openSessionWorkspace(id: string, app: WorkspaceApp): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(
    `/api/sessions/${encodeURIComponent(id)}/open-workspace`, { app },
  );
}
