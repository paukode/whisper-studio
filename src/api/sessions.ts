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

export type WorkspaceApp = 'vscode' | 'kiro' | 'finder';

export function openSessionWorkspace(id: string, app: WorkspaceApp): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(
    `/api/sessions/${encodeURIComponent(id)}/open-workspace`, { app },
  );
}

/**
 * Fire-and-forget session save using navigator.sendBeacon.
 * Used in beforeunload handlers where fetch may be cancelled.
 */
export function saveSessionBeacon(id: string, data: unknown): void {
  const url = `/api/sessions/${encodeURIComponent(id)}/beacon`;
  const blob = new Blob([JSON.stringify(data)], { type: 'application/json' });
  navigator.sendBeacon(url, blob);
}
