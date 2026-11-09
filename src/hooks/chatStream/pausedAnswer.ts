/**
 * The answer to a tool call the turn paused on: a question card or the
 * workspace folder card. ONE path for every card, so each resumes the turn
 * the same way and recovers the same way when the resume is refused.
 *
 * The answer travels as an answers array ({tool_use_id, content}) through
 * sendChatTurn's approvedToolResult, the only path that reaches the server's
 * approved_tool_result branch and restores the stashed paused turn (a bare
 * answer string would start a brand-new turn instead). It goes to the session
 * that owns the card, whatever the user views by the time the answer is ready.
 */
import { getChatStore } from '@/stores/sessionRuntimes';
import { sendChatTurn } from '../useChatStream';

export interface PausedAnswer {
  tool_use_id: string;
  content: string;
}

/**
 * Mark the card answered, resume the paused turn with `answers`, and unmark
 * it when the resume was refused before the server ran it (consent declined,
 * the parallel ceiling, a busy session), so the user can answer again instead
 * of facing a card that says answered over a turn that never resumed.
 * Marked first, so a re-render or reload meanwhile cannot send it twice.
 * Resolves to whether the turn resumed.
 */
export async function answerPausedTurn(
  sessionId: string | null,
  answers: PausedAnswer[],
  markAnswered: (answered: boolean) => void,
): Promise<boolean> {
  markAnswered(true);
  const resumed = await sendChatTurn('', {
    approvedToolResult: answers,
    sessionId: sessionId ?? undefined,
  });
  if (!resumed) markAnswered(false);
  return resumed;
}

/** Set `answered` on the question rows asking these tool calls, in the
 *  owning session's transcript, so the card keeps its state across a
 *  re-render or a restore. */
export function markQuestionsAnswered(
  sessionId: string | null,
  toolUseIds: readonly string[],
  answered: boolean,
): void {
  const chat = getChatStore(sessionId).getState();
  const ids = new Set(toolUseIds);
  let changed = false;
  const messages = chat.messages.map((m) => {
    if (m.userQuestions?.some((q) => ids.has(q.toolUseId))) {
      changed = true;
      return {
        ...m,
        userQuestions: m.userQuestions.map((q) => (ids.has(q.toolUseId) ? { ...q, answered } : q)),
      };
    }
    if (m.userQuestion && ids.has(m.userQuestion.toolUseId)) {
      changed = true;
      return { ...m, userQuestion: { ...m.userQuestion, answered } };
    }
    return m;
  });
  if (changed) chat.setMessages(messages);
}
