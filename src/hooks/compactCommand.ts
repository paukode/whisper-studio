/**
 * `/compact [note]`: summarize the whole conversation now. The server writes
 * the summary as a backend-owned `compact_summary` row (server/chat/
 * compact_route.py) and returns it; this adds the same row to the chat. From
 * then on every turn sends the model that row and what follows it
 * (chatStream/history.ts, visible_chat_history).
 */
import { post } from '@/api/client';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { UIState } from '@/stores/uiStore';
import type { ChatMessage } from '@/types/chat';
import { toError } from '@/utils/toError';
import { buildHistoryPayload } from './chatStream/history';

export async function runCompact(
  sessionId: string,
  note: string,
  model: string,
  addToast: UIState['addToast'],
): Promise<void> {
  const chat = () => getChatStore(sessionId).getState();
  if (chat().isStreaming) {
    addToast({ type: 'info', message: 'Wait for the reply to finish, then run /compact.', duration: 3000, persist: false });
    return;
  }
  const history = buildHistoryPayload(chat().messages, true, true);
  if (!history.some((m) => m.role === 'user' || m.role === 'assistant')) {
    addToast({ type: 'info', message: 'There is nothing to compact yet.', duration: 2500, persist: false });
    return;
  }
  addToast({ type: 'info', message: 'Compacting the conversation...', duration: 2500, persist: false });
  try {
    const row = await post<ChatMessage>(
      `/api/sessions/${encodeURIComponent(sessionId)}/compact`,
      { history, note, model },
    );
    chat().addMessage(row);
    addToast({ type: 'success', message: 'Conversation compacted.', duration: 2500, persist: false });
  } catch (e) {
    addToast({ type: 'error', message: `Could not compact: ${toError(e).message}` });
  }
}
