/**
 * Clear and Reset session: the two ways a session lets go of its turn.
 *
 * ONE path each, for every control that offers it (the Clear button and
 * /clear; the More menu's Reset session), so none of them can leave a card on
 * screen whose turn is gone. A card left behind kept its buttons: it showed
 * on the empty session, and a click on it later resumed a turn nobody saw.
 */
import { resetChatSession } from '@/api/sessions';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { toError } from '@/utils/toError';
import { killSessionStream } from './streamControl';

/**
 * End the server side of the session's turn (stream slot, paused turn,
 * approval cards, queued notes) and drop its approval cards here. Resolves to
 * whether the server confirmed it. The cards go either way: the server
 * refuses to run a card of a reset turn.
 */
export async function resetSession(sessionId: string): Promise<boolean> {
  getChatStore(sessionId).getState().dropApprovals();
  try {
    await resetChatSession(sessionId);
    return true;
  } catch (err) {
    console.warn('Session reset failed:', toError(err).message);
    return false;
  }
}

/**
 * Empty the session's conversation: stop a running turn (and the commands it
 * started), end every card it waits on, and empty the transcript, which then
 * saves.
 */
export async function clearConversation(sessionId: string): Promise<void> {
  killSessionStream(sessionId);
  getChatStore(sessionId).getState().clearMessages();
  useSessionStore.getState().clearChat(sessionId);
  if (!(await resetSession(sessionId))) {
    useUIStore.getState().addToast({
      type: 'error',
      message: 'The chat is clear, but the server did not confirm it. Use Reset session if the next message says the session is busy.',
      duration: 6000,
    });
  }
}
