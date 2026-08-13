/**
 * The `history` rows every /api/chat request ships, shared by the fresh-turn
 * path and the approval-resume path.
 *
 * A leaf module (not useChatStream) because BOTH request builders need it and
 * sseStream cannot import useChatStream — useChatStream already imports
 * sseStream, so that direction would cycle.
 */
import type { ChatMessage } from '@/types/chat';

/** History rows shipped to /api/chat (capped to the last 40). Per-message
 *  attachment ids ride along so the backend can re-inject each file's content
 *  at its original position — attachments are session state, not turn state.
 *  Names only feed the "no longer available" marker for unresolvable ids.
 *
 *  `isContinuation` keeps the LAST message too: a fresh turn has just appended
 *  the user's new question (which travels as `question`, not history), while a
 *  continuation appended nothing. */
export function buildHistoryPayload(
  allMessages: ChatMessage[],
  isContinuation: boolean,
): Array<Record<string, unknown>> {
  const history = allMessages
    .slice(0, isContinuation ? allMessages.length : -1)
    .filter(m => m.role === 'user' || m.role === 'assistant')
    .map(m => ({
      role: m.role,
      content: m.content,
      ...(m.attachmentIds?.length ? { attachmentIds: m.attachmentIds } : {}),
      ...(m.attachmentNames?.length ? { attachmentNames: m.attachmentNames } : {}),
    }));
  return history.length > 40 ? history.slice(-40) : history;
}
