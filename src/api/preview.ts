import { get, post, del } from '@/api/client';

/** A running preview session as reported by GET /api/preview/sessions. */
export interface PreviewSession {
  id: string;
  url: string | null;
  port: number | null;
  process_alive: boolean | null;
  browser_started: boolean | null;
  created_at: number;
}

/** List the preview sessions the assistant currently has running. */
export async function listPreviewSessions(): Promise<PreviewSession[]> {
  const data = await get<{ sessions: PreviewSession[] }>('/api/preview/sessions');
  return data.sessions ?? [];
}

/** Start (or restart) a preview session by name, resolving its .whisper/launch.json config. */
export async function startPreviewSession(name: string): Promise<void> {
  await post('/api/preview/sessions', { name });
}

/** Stop a preview session (kills the dev server + its browser). */
export async function stopPreviewSession(name: string): Promise<void> {
  await del(`/api/preview/sessions/${encodeURIComponent(name)}`);
}

/**
 * Open the live screencast WebSocket for a preview session.
 *
 * Path is under /ws so Vite's dev proxy upgrades it as a WebSocket. Frames
 * arrive as text messages: raw base64 JPEG (no data-URI prefix).
 */
export function createScreencastSocket(name: string): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${protocol}//${window.location.host}/ws/preview/${encodeURIComponent(name)}/screencast`;
  return new WebSocket(url);
}
