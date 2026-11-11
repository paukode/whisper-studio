/**
 * The cards a session's paused turn waits on, closed when the user sends a
 * message instead of answering them.
 *
 * The message continues that same turn: the request names the closed cards
 * (`skipped_cards`), and the server resumes the paused turn with each
 * approval or folder call closed as not run and each question as not
 * answered, then the message (server/chat/resume.py). A card left open used
 * to stay on screen below the new messages with nothing behind it, and a
 * click on it later replaced whatever the session was doing (a /file upload
 * included).
 *
 * The ids stay in the store until a request that carries them is accepted
 * (useChatStream), so a send that never reached the server, such as a /file
 * upload that was stopped, hands them to the next one.
 */
import type { StoreApi } from 'zustand/vanilla';
import type { ChatState } from '@/stores/chatStore';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { ChatMessage } from '@/types/chat';
import { patchCommittedRows } from './committedRows';

/** What a closed approval or folder call's row says. */
export const SKIPPED_CARD_RESULT = 'Not run: you sent a new message instead.';

type Question = NonNullable<ChatMessage['userQuestion']>;

const waits = (q: Question): boolean => !q.answered && !q.skipped;

/** The folder card's row while it waits on the user (WorkspacePromptCard). */
const waitingFolder = (m: ChatMessage): boolean =>
  !!m.toolUse?.some((t) => t.toolName === 'ws_workspace_prompt' && t.status === 'pending');

/**
 * Close every card the session waits on and remember their ids for the next
 * request. Question and folder cards wait only in the rows since the last
 * user message: an older one belongs to a turn that is already over.
 */
export function closeWaitingCards(sessionId: string): void {
  const chat = getChatStore(sessionId);
  const ids: string[] = [];

  const { currentApproval, approvalQueue } = chat.getState();
  const approvals = [currentApproval, ...approvalQueue].filter((a) => a !== null);
  if (approvals.length > 0) {
    chat.getState().dropApprovals();
    for (const approval of approvals) {
      if (!approval.toolUseId) continue;
      ids.push(approval.toolUseId);
      closeRow(chat, sessionId, approval.toolUseId);
    }
  }

  const messages = chat.getState().messages;
  const lastUser = messages.map((m) => m.role).lastIndexOf('user');
  const folderIds: string[] = [];
  let changed = false;
  const next = messages.map((m, i) => {
    if (i <= lastUser || m.role !== 'assistant') return m;
    let row = m;
    if (m.userQuestions?.some(waits)) {
      for (const q of m.userQuestions) if (waits(q)) ids.push(q.toolUseId);
      row = { ...row, userQuestions: m.userQuestions.map((q) => (waits(q) ? { ...q, skipped: true } : q)) };
    }
    if (m.userQuestion && waits(m.userQuestion)) {
      ids.push(m.userQuestion.toolUseId);
      row = { ...row, userQuestion: { ...m.userQuestion, skipped: true } };
    }
    if (waitingFolder(m)) {
      for (const t of m.toolUse ?? []) {
        if (t.toolName === 'ws_workspace_prompt' && t.status === 'pending') folderIds.push(t.toolId);
      }
    }
    if (row !== m) changed = true;
    return row;
  });
  if (changed) chat.getState().setMessages(next);
  for (const id of folderIds) {
    ids.push(id);
    closeRow(chat, sessionId, id);
  }

  if (ids.length > 0) chat.getState().addSkippedCards(ids);
}

function closeRow(chat: StoreApi<ChatState>, sessionId: string, id: string): void {
  patchCommittedRows(chat, sessionId, id, {
    result: SKIPPED_CARD_RESULT,
    status: 'stopped',
    isError: false,
    exitCode: null,
  });
}

/**
 * Forget the session's cards without closing them on the server: their turn
 * is being replaced (an edited or resent message cuts the transcript above
 * them), so no request may resume it.
 */
export function dropWaitingCards(sessionId: string): void {
  const chat = getChatStore(sessionId).getState();
  chat.dropApprovals();
  chat.clearSkippedCards(chat.skippedCards);
}
