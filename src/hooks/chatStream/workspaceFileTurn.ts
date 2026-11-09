/**
 * A question about one workspace file: `/file:path question`, and a resend of
 * such a message (./resend). ONE path for both, so a resend uploads the file
 * again instead of sending the question without it.
 *
 * The user's message shows at once. The turn is `starting` while the file
 * uploads, and the status line names the upload, not "Thinking": the server
 * reads a document into text on the way in, which takes minutes for a very
 * large PDF. The model call fires once the upload is done, unless the user
 * stopped the turn or started another one meanwhile. Then the upload request
 * is cancelled too: it holds one of the browser's 6 connections to the app,
 * and with three turns streaming, one held upload left no connection to load
 * a session with.
 */
import { uploadWorkspaceFile } from '@/api/workspace';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useUIStore } from '@/stores/uiStore';
import type { ChatMessage } from '@/types/chat';
import { toError } from '@/utils/toError';
import { sendChatTurn } from '../useChatStream';

export function askAboutWorkspaceFile(sessionId: string | null, path: string, question: string): void {
  const fileName = path.split('/').pop() || path;
  const ts = new Date().toISOString();
  const chat = () => getChatStore(sessionId).getState();
  chat().addMessage({
    role: 'user',
    content: question,
    timestamp: ts,
    attachmentNames: [fileName],
    workspaceFile: path,
  });
  const leg = chat().startTurn('starting');
  chat().setStreamStatus(`Uploading ${fileName}…`);
  // Stopped (or replaced by another send) while the file uploads: the turn
  // no longer waits for it, and the upload stops with it.
  const stillStarting = () => chat().turnSeq === leg && chat().turnPhase === 'starting';
  const upload = new AbortController();
  const stopUpload = getChatStore(sessionId).subscribe(() => {
    if (!stillStarting()) upload.abort();
  });

  const patchMessage = (patch: Partial<ChatMessage>) => {
    const st = chat();
    const idx = st.messages.findIndex((m) => m.timestamp === ts);
    if (idx < 0) return;
    const updated = [...st.messages];
    updated[idx] = { ...updated[idx], ...patch };
    st.setMessages(updated);
  };

  void (async () => {
    let att: { id: string; filename: string };
    try {
      att = await uploadWorkspaceFile(path, upload.signal);
    } catch (e) {
      // Cancelled because the turn ended: nothing failed. The message keeps
      // its path, so a resend uploads the file again.
      if (upload.signal.aborted) return;
      const why = toError(e).message;
      chat().finishStream(undefined, leg);
      patchMessage({ content: `${question}\n\n_[Could not attach ${fileName}: ${why}]_` });
      useUIStore.getState().addToast({ type: 'error', message: `Could not attach ${fileName}: ${why}` });
      return;
    } finally {
      stopUpload();
    }
    // Record the real id on the message already shown, so a later resend
    // sends the same file without another upload.
    patchMessage({ attachmentIds: [att.id], attachmentNames: [att.filename] });
    if (!stillStarting()) return;
    await sendChatTurn(question, {
      attachmentIds: [att.id],
      attachmentNames: [att.filename],
      hideUserMessage: true,
      sessionId: sessionId ?? undefined,
    });
  })();
}
