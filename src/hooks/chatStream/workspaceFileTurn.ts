/**
 * A message with workspace files or folders from the @ menu (or the tree's
 * Add to Chat), and a resend of such a message (./resend). ONE path for both,
 * so a resend uploads the files again instead of sending the message without
 * them.
 *
 * The user's message shows at once. The turn is `starting` while the files
 * upload, and the status line names the upload, not "Thinking": the server
 * reads a document into text on the way in, which takes minutes for a very
 * large PDF. A folder carries its listing. The model call fires once every
 * upload is done, unless the user stopped the turn or started another one
 * meanwhile. Then the uploads are cancelled too, so a stopped upload never
 * keeps converting on the server.
 */
import { attachWorkspaceFolder, uploadWorkspaceFile } from '@/api/workspace';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import type { ChatMessage, WorkspaceRef } from '@/types/chat';
import { toError } from '@/utils/toError';
import { buildDisplayQuestion, sendChatTurn } from '../useChatStream';
import { closeWaitingCards } from './waitingCards';

export interface WorkspaceRefSend {
  refs: WorkspaceRef[];
  /** Files already uploaded with the paperclip or drag-drop. */
  attachmentIds?: string[];
  attachmentNames?: string[];
  forceSkill?: string;
  displayText?: string;
}

const refName = (ref: WorkspaceRef): string => {
  const name = ref.path.split('/').pop() || ref.path;
  return ref.folder ? `${name}/` : name;
};

export function sendWithWorkspaceRefs(sessionId: string | null, question: string, send: WorkspaceRefSend): void {
  const owner = sessionId ?? useSessionStore.getState().createSession();
  const uploadedIds = send.attachmentIds ?? [];
  const uploadedNames = send.attachmentNames ?? [];
  const names = send.refs.map(refName);
  const ts = new Date().toISOString();
  const chat = () => getChatStore(owner).getState();
  // The cards this message answers close now, before it shows: closed after
  // the upload, a question card above it would no longer count as waiting.
  closeWaitingCards(owner);
  chat().addMessage({
    role: 'user',
    content: buildDisplayQuestion(question, send),
    timestamp: ts,
    attachmentIds: uploadedIds,
    attachmentNames: [...uploadedNames, ...names],
    workspaceRefs: send.refs,
  });
  const leg = chat().startTurn('starting');
  chat().setStreamStatus(names.length === 1 ? `Uploading ${names[0]}…` : `Uploading ${names.length} files…`);
  // Stopped (or replaced by another send) while the files upload: the turn
  // no longer waits for them, and the uploads stop with it.
  const stillStarting = () => chat().turnSeq === leg && chat().turnPhase === 'starting';
  const upload = new AbortController();
  const stopUpload = getChatStore(owner).subscribe(() => {
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
    let atts: Array<{ id: string; filename: string }>;
    try {
      atts = await Promise.all(send.refs.map((ref) => (ref.folder
        ? attachWorkspaceFolder(ref.path, upload.signal)
        : uploadWorkspaceFile(ref.path, upload.signal))));
    } catch (e) {
      // Cancelled because the turn ended: nothing failed. The message keeps
      // its paths, so a resend uploads the files again.
      if (upload.signal.aborted) return;
      upload.abort();
      const why = toError(e).message;
      chat().finishStream(undefined, leg);
      patchMessage({ content: `${buildDisplayQuestion(question, send)}\n\n_[Could not attach ${names.join(', ')}: ${why}]_` });
      useUIStore.getState().addToast({ type: 'error', message: `Could not attach ${names.join(', ')}: ${why}` });
      return;
    } finally {
      stopUpload();
    }
    // Record the real ids on the message already shown, so a later resend
    // sends the same files without another upload.
    const attachmentIds = [...uploadedIds, ...atts.map((a) => a.id)];
    const attachmentNames = [...uploadedNames, ...atts.map((a) => a.filename)];
    patchMessage({ attachmentIds, attachmentNames });
    if (!stillStarting()) return;
    await sendChatTurn(question, {
      attachmentIds,
      attachmentNames,
      forceSkill: send.forceSkill,
      displayText: send.displayText,
      hideUserMessage: true,
      sessionId: owner,
    });
  })();
}
