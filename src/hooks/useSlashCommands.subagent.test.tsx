import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// /subagent writes its answer into the message that hosts its progress card,
// in place. Patching in place keeps the message count, which is all the
// session runtime's own save trigger keys on, so unless the run saves the
// session itself its answer lives only in memory and is lost on reload or
// eviction.
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ setTheme: vi.fn(), themeKey: 'light-taw', resolvedTheme: 'light-taw', themes: [] }),
}));

import { useSlashCommands, type UseSlashCommandsOptions } from './useSlashCommands';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

const SID = 'subagent-sess';

function makeOpts(): UseSlashCommandsOptions {
  return {
    models: [],
    setVerbosity: vi.fn(),
    setEffortLevel: vi.fn(),
    selectedModel: 'm',
    autoMemory: false,
    setAutoMemory: vi.fn(),
    openSettings: vi.fn(),
    addToast: vi.fn(),
    sessionId: SID,
    handleNativeBrowse: vi.fn(),
    handlePlanToggle: vi.fn(),
    chatStream: { send: vi.fn(), sendMidTurn: vi.fn(), abort: vi.fn() },
    slashCommands: [],
    attachWorkspaceFileAsChip: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
  } as unknown as UseSlashCommandsOptions;
}

function subagentStream(frames: unknown[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      for (const f of frames) c.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
      c.enqueue(enc.encode('data: [DONE]\n\n'));
      c.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

describe('useSlashCommands: /subagent', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });
  afterEach(() => vi.restoreAllMocks());

  it('saves its session once the final answer is in place', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      String(input).includes('/api/subagent/stream')
        ? subagentStream([{ subagent_done: { output: 'Found it in utils.py' } }])
        : new Response('{}', { status: 200 }),
    );
    // What the host message said at each save request for this session.
    const hostAtSave: string[] = [];
    const debouncedSave = vi.fn((id: string) => {
      if (id !== SID) return;
      const host = getChatStore(SID).getState().messages.find((m) => m.role === 'assistant');
      hostAtSave.push(host?.content ?? '');
    });
    useSessionStore.setState({ debouncedSave } as never);

    const { result } = renderHook(() => useSlashCommands(makeOpts()));
    expect(result.current.handleSlashCommand('/subagent find the helper')).toBe(true);

    await waitFor(() => {
      const host = getChatStore(SID).getState().messages.find((m) => m.role === 'assistant');
      expect(host?.content).toBe('Found it in utils.py');
    });
    await waitFor(() => expect(hostAtSave).toContain('Found it in utils.py'));
  });
});
