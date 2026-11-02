/**
 * The automatic session title, asked for once per session after the first
 * exchange. A failed request is logged and remembered for this window, so a
 * title model that keeps failing costs one call per session, not one per turn.
 */

const failedSessions = new Set<string>();

/** True when this window already asked for a title for the session and it failed. */
export function autoTitleFailed(sessionId: string): boolean {
  return failedSessions.has(sessionId);
}

/** Ask /api/generate-title for a title and hand a non-empty one to `onTitle`. */
export async function requestAutoTitle(
  sessionId: string,
  text: string,
  onTitle: (title: string) => void,
): Promise<void> {
  try {
    const r = await fetch('/api/generate-title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // session_id attributes the title call's cost to this session.
      body: JSON.stringify({ text, session_id: sessionId }),
    });
    // A failed title has an error body, never a title: stop on non-2xx.
    if (!r.ok) throw new Error(`title HTTP ${r.status}`);
    const d = (await r.json()) as { title?: string };
    if (d.title) onTitle(d.title);
  } catch (err) {
    failedSessions.add(sessionId);
    console.error('[Chat] Title generation failed:', err);
  }
}

/** Test hook: forget every failed session. */
export function _resetAutoTitle(): void {
  failedSessions.clear();
}
