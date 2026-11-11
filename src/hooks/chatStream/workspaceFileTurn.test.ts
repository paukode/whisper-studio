import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';

vi.mock('@/api/workspace', () => ({ uploadWorkspaceFile: vi.fn(), attachWorkspaceFolder: vi.fn() }));
vi.mock('../useChatStream', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../useChatStream')>()),
  sendChatTurn: vi.fn().mockResolvedValue(true),
}));

import { attachWorkspaceFolder, uploadWorkspaceFile } from '@/api/workspace';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useUIStore } from '@/stores/uiStore';
import { sendChatTurn } from '../useChatStream';
import { sendWithWorkspaceRefs } from './workspaceFileTurn';

const SID = 'file-turn-sess';
const chat = () => getChatStore(SID).getState();
const PDF = { path: 'docs/big.pdf' };
const SRC = { path: 'src', folder: true };

/** An upload the test finishes by hand. */
function pendingUpload(): (att: { id: string; filename: string }) => void {
  let finish: (att: { id: string; filename: string }) => void = () => {};
  vi.mocked(uploadWorkspaceFile).mockImplementation(() => new Promise((r) => { finish = r; }));
  return (att) => finish(att);
}

beforeEach(() => {
  vi.mocked(uploadWorkspaceFile).mockReset();
  vi.mocked(attachWorkspaceFolder).mockReset();
  vi.mocked(sendChatTurn).mockClear();
  chat().setMessages([]);
});

afterEach(() => {
  chat().finishStream();
  chat().setMessages([]);
});

describe('sendWithWorkspaceRefs', () => {
  it('shows the message at once and names the upload while it runs, not Thinking', () => {
    pendingUpload();
    sendWithWorkspaceRefs(SID, 'Summarize this file.', { refs: [PDF] });

    expect(chat().messages).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'Summarize this file.',
        attachmentNames: ['big.pdf'],
        workspaceRefs: [PDF],
      }),
    ]);
    expect(chat().turnPhase).toBe('starting');
    expect(chat().streamStatus).toBe('Uploading big.pdf…');
    expect(uploadWorkspaceFile).toHaveBeenCalledWith('docs/big.pdf', expect.any(AbortSignal));
  });

  it('sends files, a folder listing and the uploaded chips together, in order', async () => {
    const finish = pendingUpload();
    vi.mocked(attachWorkspaceFolder).mockResolvedValue({ id: 'dir1', filename: 'src/' });
    sendWithWorkspaceRefs(SID, 'Compare these.', {
      refs: [PDF, SRC],
      attachmentIds: ['up1'],
      attachmentNames: ['notes.txt'],
    });
    expect(chat().streamStatus).toBe('Uploading 2 files…');
    expect(chat().messages[0].attachmentNames).toEqual(['notes.txt', 'big.pdf', 'src/']);
    finish({ id: 'att1', filename: 'big.pdf' });

    await waitFor(() => expect(sendChatTurn).toHaveBeenCalled());
    expect(attachWorkspaceFolder).toHaveBeenCalledWith('src', expect.any(AbortSignal));
    expect(sendChatTurn).toHaveBeenCalledWith('Compare these.', expect.objectContaining({
      attachmentIds: ['up1', 'att1', 'dir1'],
      attachmentNames: ['notes.txt', 'big.pdf', 'src/'],
      hideUserMessage: true,
      sessionId: SID,
    }));
    expect(chat().messages[0].attachmentIds).toEqual(['up1', 'att1', 'dir1']);
  });

  it('keeps a /skill run: the bubble shows the command, the send carries the skill', async () => {
    const finish = pendingUpload();
    sendWithWorkspaceRefs(SID, 'Run the requested skill on the conversation context.', {
      refs: [PDF],
      forceSkill: 'meeting_notes',
      displayText: '',
    });
    expect(chat().messages[0].content).toBe('/meeting_notes');
    finish({ id: 'att1', filename: 'big.pdf' });

    await waitFor(() => expect(sendChatTurn).toHaveBeenCalled());
    expect(vi.mocked(sendChatTurn).mock.calls[0][1]).toMatchObject({ forceSkill: 'meeting_notes', displayText: '' });
  });

  it('a Stop while the files upload cancels the uploads and sends nothing', async () => {
    const finish = pendingUpload();
    sendWithWorkspaceRefs(SID, 'Summarize this file.', { refs: [PDF] });
    const signal = vi.mocked(uploadWorkspaceFile).mock.calls[0][1] as AbortSignal;
    expect(signal.aborted).toBe(false);
    // What the Stop button does to the session's state.
    chat().finishStream();
    // The server converts the upload: it must not outlive the turn.
    expect(signal.aborted).toBe(true);
    finish({ id: 'att1', filename: 'big.pdf' });

    await new Promise((r) => setTimeout(r, 20));
    expect(sendChatTurn).not.toHaveBeenCalled();
    expect(chat().turnPhase).toBe('idle');
  });

  it('a cancelled upload is not reported as a failure', async () => {
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');
    vi.mocked(uploadWorkspaceFile).mockImplementation((_path, signal) => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    }));
    sendWithWorkspaceRefs(SID, 'Summarize this file.', { refs: [PDF] });
    chat().finishStream();

    await new Promise((r) => setTimeout(r, 20));
    expect(addToast).not.toHaveBeenCalled();
    expect(chat().messages[0].content).toBe('Summarize this file.');
    addToast.mockRestore();
  });

  it('a failed upload ends the turn, stops the other uploads and says why', async () => {
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');
    vi.mocked(uploadWorkspaceFile).mockRejectedValue(new Error('File big.pdf exceeds 50MB limit'));
    vi.mocked(attachWorkspaceFolder).mockImplementation(() => new Promise(() => {}));
    sendWithWorkspaceRefs(SID, 'Summarize this file.', { refs: [PDF, SRC] });

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({
        type: 'error',
        message: 'Could not attach big.pdf, src/: File big.pdf exceeds 50MB limit',
      }),
    );
    const folderSignal = vi.mocked(attachWorkspaceFolder).mock.calls[0][1] as AbortSignal;
    expect(folderSignal.aborted).toBe(true);
    expect(chat().turnPhase).toBe('idle');
    expect(sendChatTurn).not.toHaveBeenCalled();
    addToast.mockRestore();
  });
});
