/**
 * The `history` rows every /api/chat request ships, shared by the fresh-turn
 * path and the approval-resume path.
 *
 * A leaf module (not useChatStream) because BOTH request builders need it and
 * sseStream cannot import useChatStream — useChatStream already imports
 * sseStream, so that direction would cycle.
 */
import type { ChatMessage } from '@/types/chat';

/** Backend-owned rows the model must read: agent reports that landed with no
 *  turn running, the wake turn's answer to them, and cross-session messages.
 *  The server relabels each as a user or assistant turn
 *  (visible_chat_history); every other UI-only row (cron_event, task_event)
 *  stays out of the prompt. */
const MODEL_ROWS: ReadonlySet<ChatMessage['role']> = new Set([
  'agent_report',
  'agent_answer',
  'session_message',
]);

/** How many user and assistant rows travel. The backend rows between them
 *  ride along without counting, so a burst of reports never pushes the
 *  conversation out of the window. */
const CONVERSATION_ROWS = 40;

const isConversation = (m: ChatMessage) => m.role === 'user' || m.role === 'assistant';

/** History rows shipped to /api/chat (the last 40 user and assistant rows,
 *  plus the backend rows among them). Per-message attachment ids ride along
 *  so the backend can re-inject each file's content at its original position
 *  (attachments are session state, not turn state). Names only feed the "no
 *  longer available" marker for unresolvable ids.
 *
 *  `isContinuation` keeps the LAST message too: a fresh turn has just appended
 *  the user's new question (which travels as `question`, not history), while a
 *  continuation appended nothing. */
export function buildHistoryPayload(
  allMessages: ChatMessage[],
  isContinuation: boolean,
): Array<Record<string, unknown>> {
  const rows = allMessages
    .slice(0, isContinuation ? allMessages.length : -1)
    .filter(m => isConversation(m) || MODEL_ROWS.has(m.role));
  let start = 0;
  let counted = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (isConversation(rows[i]) && ++counted === CONVERSATION_ROWS) {
      start = i;
      break;
    }
  }
  return rows.slice(start).map(m => ({
    role: m.role,
    content: m.content,
    ...(m.attachmentIds?.length ? { attachmentIds: m.attachmentIds } : {}),
    ...(m.attachmentNames?.length ? { attachmentNames: m.attachmentNames } : {}),
    ...(m.agentReport ? { agentReport: m.agentReport } : {}),
    ...(m.agentAnswer ? { agentAnswer: m.agentAnswer } : {}),
    ...(m.sessionMessage ? { sessionMessage: m.sessionMessage } : {}),
  }));
}
