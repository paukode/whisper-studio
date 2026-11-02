import { describe, expect, it, vi, afterEach } from 'vitest';
import { activeMs, beginWait, endAllWaits, endWait, formatWait, waitSummary, waitedMs, type WaitState } from './waitClock';
import { createChatStore } from '@/stores/chatStore';
import type { PendingApproval } from '@/stores/chatStore';

const empty: WaitState = { waitSpans: [], waitReasons: {} };

describe('wait clock', () => {
  it('thinking time is the wall time minus the waits', () => {
    let s = beginWait(empty, 'approval', 'user', 1_000);
    s = endWait(s, 'approval', 61_000);
    expect(activeMs(0, 100_000, s.waitSpans)).toBe(100_000 - 60_000);
    expect(waitedMs(0, 100_000, s.waitSpans, 'user')).toBe(60_000);
  });

  it('an open wait keeps growing and never counts as thinking', () => {
    const s = beginWait(empty, 'tool:t1', 'sleep', 10_000);
    expect(activeMs(0, 50_000, s.waitSpans)).toBe(10_000);
    expect(waitedMs(0, 50_000, s.waitSpans, 'sleep')).toBe(40_000);
  });

  it('overlapping waits count once', () => {
    let s = beginWait(empty, 'approval', 'user', 0);
    s = beginWait(s, 'tool:a', 'sleep', 5_000);
    s = endWait(s, 'tool:a', 15_000);
    s = endWait(s, 'approval', 20_000);
    expect(activeMs(0, 30_000, s.waitSpans)).toBe(10_000);
  });

  it('two reasons of one kind share a span until the last one ends', () => {
    let s = beginWait(empty, 'tool:a', 'sleep', 0);
    s = beginWait(s, 'tool:b', 'sleep', 1_000);
    s = endWait(s, 'tool:a', 2_000);
    expect(s.waitSpans.filter((w) => w.end === null)).toHaveLength(1);
    s = endWait(s, 'tool:b', 3_000);
    expect(waitedMs(0, 10_000, s.waitSpans, 'sleep')).toBe(3_000);
  });

  it('only the part of a wait after the timer start counts', () => {
    let s = beginWait(empty, 'approval', 'user', 0);
    s = endWait(s, 'approval', 10_000);
    expect(activeMs(5_000, 20_000, s.waitSpans)).toBe(10_000);
  });

  it('endAllWaits closes every open span', () => {
    let s = beginWait(empty, 'approval', 'user', 0);
    s = endAllWaits(s, 4_000);
    expect(s.waitReasons).toEqual({});
    expect(waitedMs(0, 100_000, s.waitSpans, 'user')).toBe(4_000);
  });

  it('formats waits in whole units and names each kind', () => {
    expect(formatWait(41_200)).toBe('41s');
    expect(formatWait(18 * 60_000)).toBe('18m');
    expect(formatWait(65 * 60_000)).toBe('1h 05m');
    let s = beginWait(empty, 'approval', 'user', 0);
    s = endWait(s, 'approval', 18 * 60_000);
    expect(waitSummary(0, 19 * 60_000, s.waitSpans)).toBe('waited on you 18m');
    expect(waitSummary(0, 1_000, [])).toBe('');
  });
});

describe('chat store wait tracking', () => {
  afterEach(() => vi.restoreAllMocks());

  const approval = { toolUseId: 'x', action: 'a', category: 'c', preview: 'command', summary: '', payload: {}, alwaysAsks: false, sessionId: 's', turnSettings: {} } as unknown as PendingApproval;

  it('an approval card opens a user wait and answering it closes the wait', () => {
    const now = vi.spyOn(performance, 'now');
    now.mockReturnValue(100);
    const store = createChatStore();
    store.getState().setStreaming(true);
    now.mockReturnValue(1_000);
    store.getState().enqueueApproval(approval);
    now.mockReturnValue(601_000);
    store.getState().showNextApproval();
    now.mockReturnValue(611_100);
    const st = store.getState();
    expect(activeMs(st.thinkingStartTime ?? 0, 611_100, st.waitSpans)).toBe(11_000);
    store.getState().setThinkingStop();
    expect(store.getState().thinkingElapsedMs).toBe(11_000);
  });

  it('a sleep tool waits while it runs', () => {
    const now = vi.spyOn(performance, 'now');
    now.mockReturnValue(0);
    const store = createChatStore();
    store.getState().setStreaming(true);
    store.getState().addStreamToolUse({ toolId: 't1', toolName: 'sleep', input: {}, status: 'running' });
    now.mockReturnValue(30_000);
    store.getState().updateStreamToolUse('t1', { status: 'complete' });
    const st = store.getState();
    expect(st.waitReasons).toEqual({});
    expect(waitedMs(0, 40_000, st.waitSpans, 'sleep')).toBe(30_000);
  });

  it('a tool that works is thinking time', () => {
    const store = createChatStore();
    store.getState().setStreaming(true);
    store.getState().addStreamToolUse({ toolId: 't1', toolName: 'read_file', input: {}, status: 'running' });
    expect(store.getState().waitSpans).toEqual([]);
  });
});
