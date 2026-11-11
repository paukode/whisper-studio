/**
 * Send a user message again: Regenerate (on the message or on the turn that
 * answers it) and an edit of the message. ONE path for all three, so each
 * keeps the message's files the same way.
 *
 * The transcript is cut at the message, and the send adds it again. Files
 * travel by id when they were uploaded. A message whose workspace files never
 * finished uploading uploads them again (./workspaceFileTurn). A message
 * that names a file the app has no copy of is not sent without it: the user
 * is told instead, because the model would answer about some other file.
 * The cut also ends the turn a card waits on, so its cards go without
 * resuming it (./waitingCards).
 */
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { sendChatTurn } from '../useChatStream';
import { dropWaitingCards } from './waitingCards';
import { sendWithWorkspaceRefs } from './workspaceFileTurn';

export function resendUserMessage(index: number, content?: string): void {
  const sessionId = useSessionStore.getState().currentSessionId;
  const chat = getChatStore(sessionId).getState();
  const message = chat.messages[index];
  if (message?.role !== 'user') return;
  const text = content ?? message.content;
  const names = message.attachmentNames ?? [];
  const ids = message.attachmentIds ?? [];

  if (ids.length < names.length) {
    if (!message.workspaceRefs?.length) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Not sent: ${names.join(', ')} did not finish uploading. Attach the file again.`,
      });
      return;
    }
    if (sessionId) dropWaitingCards(sessionId);
    chat.deleteMessagesFrom(index);
    // The workspace files come last on the message: the names before them
    // belong to the files that were already uploaded.
    sendWithWorkspaceRefs(sessionId, text, {
      refs: message.workspaceRefs,
      attachmentIds: ids,
      attachmentNames: names.slice(0, ids.length),
    });
    return;
  }

  if (sessionId) dropWaitingCards(sessionId);
  chat.deleteMessagesFrom(index);
  void sendChatTurn(text, {
    attachmentIds: message.attachmentIds,
    attachmentNames: message.attachmentNames,
    sessionId: sessionId ?? undefined,
  });
}
