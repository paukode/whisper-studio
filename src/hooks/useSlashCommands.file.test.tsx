import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// /file:path question keeps the path's case (case-sensitive volumes) and the
// question's line breaks, and a failed upload says why.
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light', resolvedTheme: 'light', themes: [] }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { getChatStore } from '@/stores/sessionRuntimes';

function makeOpts(overrides: Partial<UseSlashCommandsOptions>): UseSlashCommandsOptions {
  return {
    models: [],
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: '',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast: vi.fn(),
    sessionId: 'file-sess',
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() },
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
    ...overrides,
  } as unknown as UseSlashCommandsOptions;
}

describe('useSlashCommands: /file:path question', () => {
  afterEach(() => {
    getChatStore('file-sess').getState().setMessages([]);
    getChatStore('file-sess').getState().finishStream();
  });

  it('keeps the path case and the question line breaks', async () => {
    const uploadWorkspaceFile = vi.fn().mockResolvedValue({ id: 'att1', filename: 'App.tsx' });
    const send = vi.fn();
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({
        uploadWorkspaceFile,
        chatStream: { send, sendMidTurn: vi.fn(), abort: vi.fn() } as unknown as UseSlashCommandsOptions['chatStream'],
      })),
    );

    result.current.handleSlashCommand('/file:src/App.tsx Explain\n\nline two');

    expect(uploadWorkspaceFile).toHaveBeenCalledWith('src/App.tsx');
    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(send.mock.calls[0][0]).toBe('Explain\n\nline two');
  });

  it('a failed upload shows the reason', async () => {
    const uploadWorkspaceFile = vi.fn().mockRejectedValue(new Error('File App.tsx exceeds 50MB limit'));
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts({ uploadWorkspaceFile, addToast })));

    result.current.handleSlashCommand('/file:src/App.tsx Explain');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({
        type: 'error',
        message: 'Could not attach App.tsx: File App.tsx exceeds 50MB limit',
      }),
    );
  });

  it('shows the turn as preparing while the file uploads', async () => {
    let finishUpload: (v: { id: string; filename: string }) => void = () => {};
    const uploadWorkspaceFile = vi.fn(() => new Promise<{ id: string; filename: string }>((r) => { finishUpload = r; }));
    const send = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({
        uploadWorkspaceFile,
        chatStream: { send, sendMidTurn: vi.fn(), abort: vi.fn() } as unknown as UseSlashCommandsOptions['chatStream'],
      })),
    );

    result.current.handleSlashCommand('/file:src/App.tsx Explain');

    expect(getChatStore('file-sess').getState().turnPhase).toBe('preparing');
    expect(getChatStore('file-sess').getState().isStreaming).toBe(true);
    finishUpload({ id: 'att1', filename: 'App.tsx' });
    await waitFor(() => expect(send).toHaveBeenCalled());
    // The send goes to the session that started it.
    expect(send.mock.calls[0][1]).toEqual(expect.objectContaining({ sessionId: 'file-sess' }));
  });

  it('a Stop while the file uploads sends nothing once the upload lands', async () => {
    let finishUpload: (v: { id: string; filename: string }) => void = () => {};
    const uploadWorkspaceFile = vi.fn(() => new Promise<{ id: string; filename: string }>((r) => { finishUpload = r; }));
    const send = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({
        uploadWorkspaceFile,
        chatStream: { send, sendMidTurn: vi.fn(), abort: vi.fn() } as unknown as UseSlashCommandsOptions['chatStream'],
      })),
    );

    result.current.handleSlashCommand('/file:src/App.tsx Explain');
    // What the Stop button does to the session's state.
    getChatStore('file-sess').getState().finishStream();
    finishUpload({ id: 'att1', filename: 'App.tsx' });

    await waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(send).not.toHaveBeenCalled();
    expect(getChatStore('file-sess').getState().turnPhase).toBe('idle');
  });
});
