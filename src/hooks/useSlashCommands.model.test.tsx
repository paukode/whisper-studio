import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light-taw', resolvedTheme: 'light-taw', themes: [] }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { useSettingsStore, type ModelEntry } from '@/stores/settingsStore';

function makeOpts(models: ModelEntry[], addToast = vi.fn()): UseSlashCommandsOptions {
  return {
    models,
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: models[0]?.key ?? '',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast,
    sessionId: null,
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: {} as UseSlashCommandsOptions['chatStream'],
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
  } as unknown as UseSlashCommandsOptions;
}

const messages = (addToast: ReturnType<typeof vi.fn>) =>
  addToast.mock.calls.map((c) => (c[0] as { message: string }).message);

describe('useSlashCommands: /model usage', () => {
  it('lists only the models on offer', () => {
    const addToast = vi.fn();
    const models = [{ key: 'local_gemma', name: 'Gemma', is_local: true }];
    const { result } = renderHook(() => useSlashCommands(makeOpts(models, addToast)));
    result.current.handleSlashCommand('/model');
    expect(messages(addToast)).toEqual(['Usage: /model local_gemma']);
  });

  it('with nothing on offer in Local mode, points to Discover instead of naming cloud models', () => {
    useSettingsStore.setState({ needsLocalModel: true });
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts([], addToast)));
    result.current.handleSlashCommand('/model');
    const [message] = messages(addToast);
    expect(message).toMatch(/Settings > Models > Discover/);
    expect(message).not.toMatch(/opus|sonnet|haiku/i);
  });
});
