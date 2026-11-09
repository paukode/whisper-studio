import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// /rename shows the new title only once the server stored it: a failed
// PATCH says why and keeps the old title.
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light', resolvedTheme: 'light', themes: [] }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { useSessionStore } from '@/stores/sessionStore';

function makeOpts(addToast: ReturnType<typeof vi.fn>): UseSlashCommandsOptions {
  return {
    models: [],
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: '',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast,
    sessionId: 'rename-sess',
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() },
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
  } as unknown as UseSlashCommandsOptions;
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('useSlashCommands: /rename', () => {
  afterEach(() => vi.restoreAllMocks());

  it('renames after the server stored the title', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(200, { ok: true }));
    const update = vi.spyOn(useSessionStore.getState(), 'updateSessionTitle');
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts(addToast)));

    result.current.handleSlashCommand('/rename Release notes');

    await waitFor(() => expect(update).toHaveBeenCalledWith('rename-sess', 'Release notes', true));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/rename-sess/title', expect.objectContaining({ method: 'PATCH' }));
  });

  it('a failed PATCH keeps the old title and says why', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(500, { error: 'database is locked' }));
    const update = vi.spyOn(useSessionStore.getState(), 'updateSessionTitle');
    update.mockClear();
    const addToast = vi.fn();
    const { result } = renderHook(() => useSlashCommands(makeOpts(addToast)));

    result.current.handleSlashCommand('/rename Release notes');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith({ type: 'error', message: 'Could not rename the session: database is locked' }),
    );
    expect(update).not.toHaveBeenCalled();
  });
});
