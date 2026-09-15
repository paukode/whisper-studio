import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

// /goal follows Claude Code semantics: the goal text is BOTH the completion
// gate and the first message. Arming the gate alone left the composer empty
// and nothing running, which read as a swallowed message.
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light-taw', resolvedTheme: 'light-taw', themes: [] }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { useGoalStore } from '@/stores/goalStore';
import { getChatStore } from '@/stores/sessionRuntimes';

function makeOpts(overrides: Partial<UseSlashCommandsOptions> = {}): UseSlashCommandsOptions {
  return {
    models: [],
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: '',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast: vi.fn(),
    sessionId: 'goal-sess',
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() },
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
    ...overrides,
  } as unknown as UseSlashCommandsOptions;
}

describe('useSlashCommands – /goal', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    useGoalStore.getState().clearGoal('goal-sess');
    getChatStore('goal-sess').getState().setStreaming(false);
  });
  afterEach(() => vi.restoreAllMocks());

  it('sets the goal, persists it, and sends the text as the first message', () => {
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    expect(result.current.handleSlashCommand('/goal fix the red CI pipeline')).toBe(true);

    expect(useGoalStore.getState().byId['goal-sess']?.goal).toBe('fix the red CI pipeline');
    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions/goal-sess/goal',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(chatStream.send).toHaveBeenCalledWith('fix the red CI pipeline');
    expect(chatStream.sendMidTurn).not.toHaveBeenCalled();
  });

  it('while a turn is streaming, steers that turn instead of starting a second one', () => {
    getChatStore('goal-sess').getState().setStreaming(true);
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    result.current.handleSlashCommand('/goal make it green');

    expect(chatStream.sendMidTurn).toHaveBeenCalledWith('make it green');
    expect(chatStream.send).not.toHaveBeenCalled();
  });

  it('/goal clear clears without sending anything', () => {
    useGoalStore.getState().setGoal('goal-sess', 'old', true);
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    result.current.handleSlashCommand('/goal clear');

    expect(useGoalStore.getState().byId['goal-sess']).toBeUndefined();
    expect(fetch).toHaveBeenCalledWith('/api/sessions/goal-sess/goal', { method: 'DELETE' });
    expect(chatStream.send).not.toHaveBeenCalled();
    expect(chatStream.sendMidTurn).not.toHaveBeenCalled();
  });
});
