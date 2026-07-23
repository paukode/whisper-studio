import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';

// The /theme handler must drive the in-tab theme setter (useTheme().setTheme),
// the same one the Settings/Header picker uses — NOT a StorageEvent, which does
// not fire in the tab that dispatched it and so left /theme a no-op until reload.
const { setThemeMock } = vi.hoisted(() => ({ setThemeMock: vi.fn() }));
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({
    setTheme: setThemeMock,
    themeKey: 'light-taw',
    resolvedTheme: 'light-taw',
    themes: [],
  }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';

function makeOpts(overrides: Partial<UseSlashCommandsOptions> = {}): UseSlashCommandsOptions {
  const opts = {
    models: [],
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: '',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast: vi.fn(),
    sessionId: null,
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: {} as UseSlashCommandsOptions['chatStream'],
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
    ...overrides,
  };
  return opts as unknown as UseSlashCommandsOptions;
}

describe('useSlashCommands – /theme', () => {
  beforeEach(() => setThemeMock.mockReset());

  it('calls the in-tab setTheme with the requested theme key', () => {
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts({ addToast })));

    const handled = result.current.handleSlashCommand('/theme dark');

    expect(handled).toBe(true);
    expect(setThemeMock).toHaveBeenCalledTimes(1);
    expect(setThemeMock).toHaveBeenCalledWith('dark');
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'success', message: 'Theme set to dark' }),
    );
  });

  it('passes "auto" through unresolved (ThemeProvider owns the system resolve)', () => {
    const { result } = renderHook(() => useSlashCommands(makeOpts()));

    result.current.handleSlashCommand('/theme auto');

    expect(setThemeMock).toHaveBeenCalledWith('auto');
  });

  it('accepts a variant theme key', () => {
    const { result } = renderHook(() => useSlashCommands(makeOpts()));

    result.current.handleSlashCommand('/theme dark-high-contrast');

    expect(setThemeMock).toHaveBeenCalledWith('dark-high-contrast');
  });

  it('does not call setTheme for an invalid theme and shows usage', () => {
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts({ addToast })));

    const handled = result.current.handleSlashCommand('/theme bogus');

    expect(handled).toBe(true);
    expect(setThemeMock).not.toHaveBeenCalled();
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'info', message: expect.stringContaining('Usage: /theme') }),
    );
  });
});
