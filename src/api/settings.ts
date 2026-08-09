import { post, put } from './client';

// ── Permissions ─────────────────────────────────────────────────────

export function updatePermissions(permissions: unknown): Promise<void> {
  return put<void>('/api/permissions', permissions);
}

/** One-click override for the auto-mode circuit breaker: resume classifying
 *  tool calls for the rest of the current turn instead of asking for every
 *  remaining one. Turn-scoped, in-memory on the backend — does not touch the
 *  persisted permission mode. */
export function resumeAutoMode(sessionId: string): Promise<{ resumed: boolean }> {
  return post<{ resumed: boolean }>('/api/permissions/auto-mode/resume', { session_id: sessionId });
}
