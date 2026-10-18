/**
 * Writing a call's outcome onto its committed rows. A Stop while a card
 * waited could commit a second copy of the waiting row; the card's answer
 * then patched that copy alone and the original kept "Awaiting approval".
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { ToolUseEvent } from '@/types/chat';
import { patchCommittedRows } from './committedRows';

const SID = 'sess-committed-rows';
const waiting: ToolUseEvent = {
  toolId: 'tu_2', toolName: 'ws_write_file', input: { path: 'b.py' }, result: 'Awaiting approval: Write b.py', status: 'complete',
};

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  vi.restoreAllMocks();
});

describe('patchCommittedRows', () => {
  it('patches every row that holds the call, and saves once', () => {
    const save = vi.spyOn(useSessionStore.getState(), 'debouncedSave').mockImplementation(() => {});
    getChatStore(SID).getState().setMessages([
      { role: 'assistant', content: 'Now b.py.', timestamp: 't1', toolUse: [waiting] },
      { role: 'assistant', content: '*(Stopped)*', timestamp: 't2', stopped: true, toolUse: [{ ...waiting, status: 'stopped' }] },
    ]);
    // Seeding the rows saves on its own (the runtime saves on a new row).
    save.mockClear();
    const found = patchCommittedRows(getChatStore(SID), SID, 'tu_2', {
      result: 'Wrote b.py (1 bytes).', status: 'complete', isError: false, exitCode: null,
    });
    expect(found).toBe(true);
    for (const row of getChatStore(SID).getState().messages) {
      expect(row.toolUse?.[0]).toMatchObject({ result: 'Wrote b.py (1 bytes).', status: 'complete', isError: false });
    }
    expect(save).toHaveBeenCalledTimes(1);
  });

  it('changes nothing, and saves nothing, when no row holds the call', () => {
    const save = vi.spyOn(useSessionStore.getState(), 'debouncedSave').mockImplementation(() => {});
    getChatStore(SID).getState().setMessages([{ role: 'assistant', content: 'a', timestamp: 't1', toolUse: [waiting] }]);
    save.mockClear();
    const before = getChatStore(SID).getState().messages;
    expect(patchCommittedRows(getChatStore(SID), SID, 'tu_gone', {
      result: 'x', status: 'error', isError: true, exitCode: null,
    })).toBe(false);
    expect(getChatStore(SID).getState().messages).toBe(before);
    expect(save).not.toHaveBeenCalled();
  });
});
