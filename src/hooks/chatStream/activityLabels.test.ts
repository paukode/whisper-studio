import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  groupKeyFor,
  labelCommit,
  patchCommittedLabel,
  writtenLabelFromFrame,
  type WrittenLabel,
} from './activityLabels';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { ChatMessage, ToolUseEvent } from '@/types/chat';

/**
 * Where a model-written caption goes: under the first toolId of the activity
 * group, as the committed view groups the calls, that holds one of the
 * round's calls. The stream tests (sseStream.test.ts) drive the same helpers
 * through real frames.
 */

const t = (toolId: string, toolName = 'ws_read_file', round?: number): ToolUseEvent => ({
  toolId, toolName, input: {}, status: 'complete', ...(round === undefined ? {} : { round }),
});
const row = (toolUse: ToolUseEvent[], content = ''): ChatMessage => ({
  role: 'assistant', content, timestamp: 'now', toolUse,
});

describe('writtenLabelFromFrame', () => {
  it('takes the round\'s calls, or the key alone, and refuses a blank caption', () => {
    expect(writtenLabelFromFrame({ tool_use_id: 'a', tool_use_ids: ['a', 'b'], label: ' Read two files ' }))
      .toEqual({ ids: ['a', 'b'], label: 'Read two files' });
    expect(writtenLabelFromFrame({ tool_use_id: 'a', label: 'Read a file' })).toEqual({ ids: ['a'], label: 'Read a file' });
    expect(writtenLabelFromFrame({ tool_use_id: 'a', label: '  ' })).toBeNull();
    expect(writtenLabelFromFrame({ tool_use_id: '', label: 'Read a file' })).toBeNull();
  });
});

describe('groupKeyFor', () => {
  it('keys by the first call of the activity group that holds one of the ids', () => {
    const calls = [t('r1'), t('g1', 'ws_grep'), t('s1', 'spawn_agent'), t('r2'), t('r3')];
    expect(groupKeyFor(calls, ['g1'])).toBe('r1');
    // An agent card ends one activity group and starts the next.
    expect(groupKeyFor(calls, ['r3'])).toBe('r2');
    // No activity group holds an agent call.
    expect(groupKeyFor(calls, ['s1'])).toBeNull();
    expect(groupKeyFor(calls, ['nope'])).toBeNull();
  });

  it('keys by the first call of the round when the calls name their rounds', () => {
    const calls = [t('a1', 'ws_read_file', 0), t('a2', 'ws_grep', 0), t('hook_blocked', 'hook_blocked'), t('b1', 'ws_read_file', 1)];
    expect(groupKeyFor(calls, ['a2'])).toBe('a1');
    expect(groupKeyFor(calls, ['b1'])).toBe('b1');
  });
});

describe('labelCommit', () => {
  it('takes only the captions that name the committed calls, the last one winning per group', () => {
    const pending: WrittenLabel[] = [
      { ids: ['r1'], label: 'Read the plan' },
      { ids: ['later'], label: 'Not this message' },
      { ids: ['g1'], label: 'Found every caller' },
    ];
    const message = labelCommit(row([t('r1'), t('g1', 'ws_grep')]), [], pending);
    expect(message.activityLabels).toEqual({ r1: 'Found every caller' });
    expect(pending).toEqual([{ ids: ['later'], label: 'Not this message' }]);
  });

  it('a tool-only row keys by the first call of the fold it joins; a row with prose keys by its own', () => {
    const above = [row([t('r0')], 'Let me look.'), row([t('r1')]), row([t('h1', 'hook_blocked')])];
    const toolOnly = labelCommit(row([t('r2')]), above, [{ ids: ['r2'], label: 'Traced the hook' }]);
    expect(toolOnly.activityLabels).toEqual({ r1: 'Traced the hook' });
    const withProse = labelCommit(row([t('r2')], 'Found it.'), above, [{ ids: ['r2'], label: 'Traced the hook' }]);
    expect(withProse.activityLabels).toEqual({ r2: 'Traced the hook' });
  });

  it('leaves a message no caption names untouched', () => {
    const message = row([t('r1')]);
    expect(labelCommit(message, [], [{ ids: ['x'], label: 'Other' }])).toBe(message);
  });
});

describe('patchCommittedLabel', () => {
  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    vi.restoreAllMocks();
  });

  it('patches the latest row that holds the calls in place and saves explicitly', () => {
    const sid = 'sess-patch-label';
    const chat = getChatStore(sid);
    chat.getState().setMessages([row([t('r1')]), { role: 'user', content: 'go on', timestamp: 'now' }, row([t('r1')], 'again')]);
    const save = vi.spyOn(useSessionStore.getState(), 'debouncedSave').mockImplementation(() => {});

    patchCommittedLabel(sid, { ids: ['r1'], label: 'Read the plan' });

    const rows = chat.getState().messages;
    expect(rows).toHaveLength(3);
    expect(rows[2].activityLabels).toEqual({ r1: 'Read the plan' });
    expect(rows[0].activityLabels).toBeUndefined();
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith(sid);
  });
});
