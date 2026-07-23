import { post, del } from '@/api/client';

/**
 * Create a new terminal session on the backend.
 * Returns the server-generated session ID.
 */
export async function createTerminalSession(
  cwd: string,
  cols = 80,
  rows = 24,
): Promise<{ session_id: string; cwd: string }> {
  return post<{ session_id: string; cwd: string }>('/api/terminal/create', { cwd, cols, rows });
}

/**
 * Kill a terminal session on the backend.
 */
export async function deleteTerminalSession(sessionId: string): Promise<void> {
  await del(`/api/terminal/${encodeURIComponent(sessionId)}`);
}

/**
 * Create a WebSocket connection for terminal I/O.
 *
 * The WebSocket connects to /ws/terminal/{sessionId} for bidirectional
 * PTY communication. Input is sent as text frames; output is received
 * as text frames.
 */
export function createTerminalSocket(sessionId: string): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${protocol}//${window.location.host}/ws/terminal/${encodeURIComponent(sessionId)}`;
  return new WebSocket(url);
}
