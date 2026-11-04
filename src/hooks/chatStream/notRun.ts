/**
 * Why a committed call never ran, read back from its result. A shared leaf:
 * the stream commits these results (sseStream, streamControl) and the
 * activity rows read them (components/chat/activityLabel).
 */
import type { ToolUseEvent } from '@/types/chat';

/** Placeholder result for a step the user cut short before its tool
 *  returned, so the committed row still has something to expand. */
export const STOPPED_TOOL_RESULT = '[Stopped by user]';

/** What the committed row of a call with no result and no known reason says. */
export const NOT_RUN_RESULT = 'Not run';

/** Why the group's calls did not run: 'user' when the user pressed Stop, the
 *  server's reason (its first sentence) when the turn ended on an error, or
 *  null when nothing says. */
export function notRunReason(tools: ToolUseEvent[]): 'user' | string | null {
  const stopped = tools.filter((t) => t.status === 'stopped');
  if (stopped.some((t) => t.result === STOPPED_TOOL_RESULT)) return 'user';
  const reason = stopped.map((t) => t.result?.trim()).find((r) => r && r !== NOT_RUN_RESULT);
  if (!reason) return null;
  const first = reason.match(/^.*?[.!?](?=\s|$)/);
  return first ? first[0] : reason;
}
