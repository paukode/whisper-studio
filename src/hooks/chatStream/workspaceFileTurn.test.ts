import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';

vi.mock('@/api/workspace', () => ({ uploadWorkspaceFile: vi.fn() }));
vi.mock('../useChatStream', () => ({ sendChatTurn: vi.fn().mockResolvedValue(true) }));

import { uploadWorkspaceFile } from '@/api/workspace';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useUIStore } from '@/stores/uiStore';
import { sendChatTurn } from '../useChatStream';
import { askAboutWorkspaceFile } from './workspaceFileTurn';

const SID = 'file-turn-sess';
const chat = () => getChatStore(SID).getState();

/** An upload the test finishes by hand. */
function pendingUpload(): (att: { id: string; filename: string }) => void {
  let finish: (att: { id: string; filename: string }) => void = () => {};
  vi.mocked(uploadWorkspaceFile).mockImplementation(() => new Promise((r) => { finish = r; }));
  return (att) => finish(att);
}

beforeEach(() => {
  vi.mocked(uploadWorkspaceFile).mockReset();
  vi.mocked(sendChatTurn).mockClear();
  chat().setMessages([]);
});

afterEach(() => {
  chat().finishStream();
  chat().setMessages([]);
});

describe('askAboutWorkspaceFile', () => {
  it('shows the question at once and names the upload while it runs, not Thinking', () => {
    pendingUpload();
    askAboutWorkspaceFile(SID, 'docs/big.pdf', 'Summarize this file.');

    expect(chat().messages).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'Summarize this file.',
        attachmentNames: ['big.pdf'],
        workspaceFile: 'docs/big.pdf',
      }),
    ]);
    expect(chat().turnPhase).toBe('starting');
    expect(chat().streamStatus).toBe('Uploading big.pdf…');
    expect(uploadWorkspaceFile).toHaveBeenCalledWith('docs/big.pdf', expect.any(AbortSignal));
  });

  it('sends the question with the uploaded file to the session that asked', async () => {
    const finish = pendingUpload();
    askAboutWorkspaceFile(SID, 'docs/big.pdf', 'Summarize this file.');
    finish({ id: 'att1', filename: 'big.pdf' });

    await waitFor(() => expect(sendChatTurn).toHaveBeenCalled());
    expect(sendChatTurn).toHaveBeenCalledWith('Summarize this file.', {
      attachmentIds: ['att1'],
      attachmentNames: ['big.pdf'],
      hideUserMessage: true,
      sessionId: SID,
    });
    expect(chat().messages[0].attachmentIds).toEqual(['att1']);
  });

  it('a Stop while the file uploads cancels the upload and sends nothing', async () => {
    const finish = pendingUpload();
    askAboutWorkspaceFile(SID, 'docs/big.pdf', 'Summarize this file.');
    const signal = vi.mocked(uploadWorkspaceFile).mock.calls[0][1] as AbortSignal;
    expect(signal.aborted).toBe(false);
    // What the Stop button does to the session's state.
    chat().finishStream();
    // The upload request holds a browser connection: it must not outlive the turn.
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
    askAboutWorkspaceFile(SID, 'docs/big.pdf', 'Summarize this file.');
    chat().finishStream();

    await new Promise((r) => setTimeout(r, 20));
    expect(addToast).not.toHaveBeenCalled();
    expect(chat().messages[0].content).toBe('Summarize this file.');
    addToast.mockRestore();
  });

  it('a failed upload ends the turn and says why', async () => {
    const addToast = vi.spyOn(useUIStore.getState(), 'addToast');
    vi.mocked(uploadWorkspaceFile).mockRejectedValue(new Error('File big.pdf exceeds 50MB limit'));
    askAboutWorkspaceFile(SID, 'docs/big.pdf', 'Summarize this file.');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({
        type: 'error',
        message: 'Could not attach big.pdf: File big.pdf exceeds 50MB limit',
      }),
    );
    expect(chat().turnPhase).toBe('idle');
    expect(sendChatTurn).not.toHaveBeenCalled();
    addToast.mockRestore();
  });
});
