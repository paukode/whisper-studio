import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

// /file:path question keeps the path's case (case-sensitive volumes) and the
// question's line breaks, and hands both to the one workspace-file turn
// (chatStream/workspaceFileTurn, tested there).
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light', resolvedTheme: 'light', themes: [] }),
}));
vi.mock('@/hooks/chatStream/workspaceFileTurn', () => ({ askAboutWorkspaceFile: vi.fn() }));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { askAboutWorkspaceFile } from '@/hooks/chatStream/workspaceFileTurn';

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
    ...overrides,
  } as unknown as UseSlashCommandsOptions;
}

describe('useSlashCommands: /file:path', () => {
  it('asks about the file with the path case and the question line breaks kept', () => {
    const { result } = renderHook(() => useSlashCommands(makeOpts({})));

    result.current.handleSlashCommand('/file:src/App.tsx Explain\n\nline two');

    expect(askAboutWorkspaceFile).toHaveBeenCalledWith('file-sess', 'src/App.tsx', 'Explain\n\nline two');
  });

  it('with no question attaches the file as a chip and sends nothing', () => {
    const attachWorkspaceFileAsChip = vi.fn();
    vi.mocked(askAboutWorkspaceFile).mockClear();
    const { result } = renderHook(() => useSlashCommands(makeOpts({ attachWorkspaceFileAsChip })));

    result.current.handleSlashCommand('/file:src/App.tsx');

    expect(attachWorkspaceFileAsChip).toHaveBeenCalledWith('src/App.tsx');
    expect(askAboutWorkspaceFile).not.toHaveBeenCalled();
  });
});
