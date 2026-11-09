import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// /goal follows Claude Code semantics: the goal text is BOTH the completion
// gate and the first message. Arming the gate alone left the composer empty
// and nothing running, which read as a swallowed message.
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light', resolvedTheme: 'light', themes: [] }),
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
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response('{"ok":true}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    useGoalStore.getState().clearGoal('goal-sess');
    getChatStore('goal-sess').getState().finishStream();
  });
  afterEach(() => vi.restoreAllMocks());

  it('sets the goal, persists it, and sends the text as the first message', async () => {
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    expect(result.current.handleSlashCommand('/goal fix the red CI pipeline')).toBe(true);

    await waitFor(() => expect(chatStream.send).toHaveBeenCalledWith('fix the red CI pipeline'));
    expect(useGoalStore.getState().byId['goal-sess']?.goal).toBe('fix the red CI pipeline');
    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions/goal-sess/goal',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(chatStream.sendMidTurn).not.toHaveBeenCalled();
  });

  it('while a turn is streaming, steers that turn instead of starting a second one', async () => {
    getChatStore('goal-sess').getState().startTurn('streaming');
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    result.current.handleSlashCommand('/goal make it green');

    await waitFor(() => expect(chatStream.sendMidTurn).toHaveBeenCalledWith('make it green'));
    expect(chatStream.send).not.toHaveBeenCalled();
  });

  it('/goal clear clears without sending anything', async () => {
    useGoalStore.getState().setGoal('goal-sess', 'old', true);
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    result.current.handleSlashCommand('/goal clear');

    await waitFor(() => expect(useGoalStore.getState().byId['goal-sess']).toBeUndefined());
    expect(fetch).toHaveBeenCalledWith('/api/sessions/goal-sess/goal', expect.objectContaining({ method: 'DELETE' }));
    expect(chatStream.send).not.toHaveBeenCalled();
    expect(chatStream.sendMidTurn).not.toHaveBeenCalled();
  });

  it('a refused goal says why and sends nothing', async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      new Response('{"error":"goal is required"}', { status: 400, headers: { 'Content-Type': 'application/json' } }),
    );
    const addToast = vi.fn();
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ addToast, chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );

    result.current.handleSlashCommand('/goal ship it');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({ type: 'error', message: 'Could not set the goal: goal is required' }),
    );
    expect(useGoalStore.getState().byId['goal-sess']).toBeUndefined();
    expect(chatStream.send).not.toHaveBeenCalled();
  });

  it('a failed clear keeps the goal and says why', async () => {
    useGoalStore.getState().setGoal('goal-sess', 'old', true);
    vi.mocked(fetch).mockImplementation(async () =>
      new Response('{"error":"database is locked"}', { status: 500, headers: { 'Content-Type': 'application/json' } }),
    );
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts({ addToast })));

    result.current.handleSlashCommand('/goal clear');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({ type: 'error', message: 'Could not clear the goal: database is locked' }),
    );
    expect(useGoalStore.getState().byId['goal-sess']?.goal).toBe('old');
  });
});

describe('useSlashCommands – /goal gate', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{"ok":true,"gates":[{"command":"npm test","failures":0}]}', { status: 200 }),
    );
    useGoalStore.getState().clearGoal('goal-sess');
    getChatStore('goal-sess').getState().finishStream();
  });
  afterEach(() => vi.restoreAllMocks());

  it('/goal gate add posts the command and never sends a chat message', async () => {
    const chatStream = { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() };
    const { result } = renderHook(() =>
      useSlashCommands(makeOpts({ chatStream: chatStream as unknown as UseSlashCommandsOptions['chatStream'] })),
    );
    expect(result.current.handleSlashCommand('/goal gate add npm test -- --run')).toBe(true);
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions/goal-sess/goal/gates',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ command: 'npm test -- --run' }) }),
    );
    expect(chatStream.send).not.toHaveBeenCalled();
  });

  it('/goal gate lists, /goal gate remove N deletes, /goal gate clear wipes', async () => {
    const { result } = renderHook(() => useSlashCommands(makeOpts()));
    result.current.handleSlashCommand('/goal gate');
    result.current.handleSlashCommand('/goal gate remove 2');
    result.current.handleSlashCommand('/goal gate clear');
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledWith('/api/sessions/goal-sess/goal/gates');
    expect(fetch).toHaveBeenCalledWith('/api/sessions/goal-sess/goal/gates/2', { method: 'DELETE' });
    expect(fetch).toHaveBeenCalledWith('/api/sessions/goal-sess/goal/gates', { method: 'DELETE' });
  });
});
