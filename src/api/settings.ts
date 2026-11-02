import { post, put } from './client';

// ── Permissions ─────────────────────────────────────────────────────

/** Persist the permission mode (permissions.json). Rejects when the server
 *  refuses or cannot be reached, so callers keep the old mode on screen. */
export function putPermissionMode(mode: string): Promise<{ mode: string }> {
  return put<{ mode: string }>('/api/permissions/mode', { mode });
}

/** One-click override for the auto-mode circuit breaker: resume classifying
 *  tool calls for the rest of the current turn instead of asking for every
 *  remaining one. Turn-scoped, in-memory on the backend — does not touch the
 *  persisted permission mode. */
export function resumeAutoMode(sessionId: string): Promise<{ resumed: boolean }> {
  return post<{ resumed: boolean }>('/api/permissions/auto-mode/resume', { session_id: sessionId });
}
