/**
 * The parallel-sessions contract: per-session stores are isolated, the
 * active wrapper re-binds on switch, runtime subscriptions save the right
 * session, and eviction never touches busy sessions.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  MAX_LIVE_RUNTIMES,
  dropRuntime,
  getChatStore,
  getRuntime,
  getTranscriptionStore,
  hasRuntime,
  maybeEvictIdle,
  useActiveChatStore,
  useRuntimeIndex,
} from './sessionRuntimes';
import { useSessionStore } from './sessionStore';
import { useRecordingStore } from './recordingStore';

function resetAll() {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
  useRecordingStore.setState({ recordingSessionId: null });
}

beforeEach(resetAll);
afterEach(() => {
  resetAll();
  vi.restoreAllMocks();
});

describe('runtime isolation', () => {
  it('gives each session its own chat and transcription stores', () => {
    getChatStore('a').getState().addMessage({ role: 'user', content: 'only in A', timestamp: 't1' });
    getTranscriptionStore('a').getState().addSegment({
      id: 's1', speaker: 'Speaker 1', text: 'hello A', timestamp: 1, edited: false,
    });

    expect(getChatStore('b').getState().messages).toHaveLength(0);
    expect(getTranscriptionStore('b').getState().segments).toHaveLength(0);
    expect(getChatStore('a').getState().messages[0].content).toBe('only in A');
  });

  it('returns the same store instance for the same id', () => {
    expect(getChatStore('a')).toBe(getChatStore('a'));
    expect(getChatStore('a')).not.toBe(getChatStore('b'));
  });

  it('tracks live ids in the reactive index', () => {
    getRuntime('a');
    getRuntime('b');
    expect(useRuntimeIndex.getState().liveIds.sort()).toEqual(['a', 'b']);
    dropRuntime('a');
    expect(useRuntimeIndex.getState().liveIds).toEqual(['b']);
  });
});

describe('useActiveChatStore', () => {
  it('re-binds to the new session store when currentSessionId changes', () => {
    getChatStore('a').getState().addMessage({ role: 'user', content: 'A msg', timestamp: 't' });
    useSessionStore.setState({ currentSessionId: 'a' });

    const { result } = renderHook(() => useActiveChatStore((s) => s.messages.length));
    expect(result.current).toBe(1);

    act(() => {
      useSessionStore.setState({ currentSessionId: 'b' });
    });
    expect(result.current).toBe(0);

    // A background write to A must NOT re-render the B-bound hook value.
    act(() => {
      getChatStore('a').getState().addMessage({ role: 'user', content: 'more A', timestamp: 't2' });
    });
    expect(result.current).toBe(0);
  });
});

describe('self-saving subscriptions', () => {
  it('debounce-saves the OWNING session when its messages change', () => {
    const spy = vi.spyOn(useSessionStore.getState(), 'debouncedSave').mockImplementation(() => {});
    useSessionStore.setState({ debouncedSave: spy } as never);

    getChatStore('bg-session').getState().addMessage({
      role: 'user', content: 'background write', timestamp: 't',
    });
    expect(spy).toHaveBeenCalledWith('bg-session');
  });

  it('syncs chat history with the right session id when ITS stream ends', async () => {
    vi.useFakeTimers();
    const spy = vi.fn();
    useSessionStore.setState({ syncChatHistory: spy } as never);

    const chat = getChatStore('streamer');
    chat.getState().addMessage({ role: 'user', content: 'q', timestamp: 't' });
    chat.getState().setStreaming(true);
    chat.getState().finishStream({ role: 'assistant', content: 'done', timestamp: 't2' });
    await vi.advanceTimersByTimeAsync(300);

    expect(spy).toHaveBeenCalledWith('streamer', expect.any(Array));
    vi.useRealTimers();
  });
});

describe('eviction', () => {
  function liveCount() {
    return useRuntimeIndex.getState().liveIds.length;
  }

  it('evicts the oldest idle hydrated session beyond the cap', () => {
    const save = vi.fn();
    useSessionStore.setState({ saveSession: save } as never);
    const ids = ['e1', 'e2', 'e3', 'e4'];
    ids.forEach((id, i) => {
      const entry = getRuntime(id);
      entry.hydrated = true;
      entry.lastUsed = i; // e1 is oldest
    });
    expect(liveCount()).toBe(4);
    maybeEvictIdle();
    expect(liveCount()).toBe(MAX_LIVE_RUNTIMES);
    expect(hasRuntime('e1')).toBe(false);
    expect(save).toHaveBeenCalledWith('e1');
  });

  it('never evicts streaming, recording, current, or approval sessions', () => {
    useSessionStore.setState({ saveSession: vi.fn() } as never);
    const entries = ['cur', 'stream', 'rec', 'appr'].map((id) => {
      const e = getRuntime(id);
      e.hydrated = true;
      e.lastUsed = 0;
      return e;
    });
    useSessionStore.setState({ currentSessionId: 'cur' });
    entries[1].chat.getState().setStreaming(true);
    useRecordingStore.setState({ recordingSessionId: 'rec' });
    entries[3].chat.setState({
      currentApproval: {
        toolUseId: 'x', action: 'write', category: 'write', preview: 'text',
        summary: 's', payload: {}, sessionId: 'appr',
      },
    });

    maybeEvictIdle();
    expect(useRuntimeIndex.getState().liveIds.sort()).toEqual(['appr', 'cur', 'rec', 'stream']);
  });

  it('dropRuntime aborts the in-flight stream and unsubscribes', () => {
    const entry = getRuntime('doomed');
    const controller = new AbortController();
    entry.abort = controller;
    dropRuntime('doomed');
    expect(controller.signal.aborted).toBe(true);
    expect(hasRuntime('doomed')).toBe(false);
  });
});
