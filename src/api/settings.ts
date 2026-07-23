import { put } from './client';

// ── Permissions ─────────────────────────────────────────────────────

export function updatePermissions(permissions: unknown): Promise<void> {
  return put<void>('/api/permissions', permissions);
}
