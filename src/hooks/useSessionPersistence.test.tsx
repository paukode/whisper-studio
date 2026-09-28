/**
 * Saves of an opened session. Nothing is sent for a session the server
 * already holds as is: not the save that loading triggers, not the one on
 * switching away, not the 30s loop, not the unload beacon. An edit, or a
 * save that failed, goes out. And what a save would send right after
 * loading is exactly what was stored, so even a retry keeps updated_at.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { Session } from '@/types/session';

vi.mock('@/api/sessions', () => ({
  getSessions: vi.fn(),
  getSession: vi.fn(),
  createSession: vi.fn(),
  updateSession: vi.fn(),
  deleteSession: vi.fn(),
  bulkDeleteSessions: vi.fn(),
  setSessionFlags: vi.fn(),
  branchSession: vi.fn(),
}));

import * as sessionsApi from '@/api/sessions';
import { sessionSaveBody, useSessionStore } from '@/stores/sessionStore';
import {
  dropRuntime,
  getChatStore,
  getTranscriptionStore,
  useRuntimeIndex,
} from '@/stores/sessionRuntimes';
import { useSessionPersistence } from './useSessionPersistence';

/** GET /api/sessions/{id} for a session last touched two weeks ago. */
const STORED = {
  id: 'old',
  title: 'Repository Branches List',
  customTitle: false,
  generatedTitle: true,
  createdAt: '2026-09-11T19:40:00.000Z',
  date: '2026-09-11T19:57:18.168Z',
  chatHistory: [
    { role: 'user', content: 'list the branches', timestamp: '2026-09-11T19:41:00.000Z' },
    {
      role: 'assistant',
      content: 'Here they are. Delete the stale ones?',
      timestamp: '2026-09-11T19:42:00.000Z',
      toolUse: [{ name: 'git_branches', input: { remote: true }, output: 'main' }],
      userQuestion: { question: 'Delete them?', options: ['Yes', 'No'], toolUseId: 'tu_1' },
    },
    { role: 'cron_event', content: '', timestamp: '2026-09-12T08:00:00.000Z', cronEvent: {} },
  ],
  segments: [
    {
      id: 'g1', speaker: 'SPEAKER_00', text: 'hello', timestamp: 1.5, edited: false,
      translations: [{ chunkId: 1, text: 'hej', target: 'pl' }],
    },
  ],
  speakerNames: { SPEAKER_00: 'Marta' },
};

/** What a save carries besides its timestamp, as it goes over the wire. */
function content(s: Partial<Record<keyof Session, unknown>>) {
  const { title, customTitle, generatedTitle, createdAt, chatHistory, segments, speakerNames } = s;
  return JSON.parse(
    JSON.stringify({ title, customTitle, generatedTitle, createdAt, chatHistory, segments, speakerNames }),
  );
}

function savesOf(id: string) {
  return vi.mocked(sessionsApi.updateSession).mock.calls
    .filter(([sid]) => sid === id)
    .map(([, payload]) => payload);
}

/** An edit that leaves the message count alone (no debounced save). */
function answerQuestion() {
  const chat = getChatStore('old');
  chat.setState({
    messages: chat.getState().messages.map((m) =>
      m.userQuestion ? { ...m, userQuestion: { ...m.userQuestion, answered: true } } : m,
    ),
  });
}

/** Advance fake time with the hook's state updates wrapped in act(). */
const tick = (ms: number) => act(async () => {
  await vi.advanceTimersByTimeAsync(ms);
});

const sendBeacon = vi.fn((..._args: [string, Blob]) => true);
let unmount: () => void;

beforeEach(async () => {
  vi.useFakeTimers();
  Object.defineProperty(navigator, 'sendBeacon', { value: sendBeacon, configurable: true });
  vi.mocked(sessionsApi.getSession).mockImplementation(
    async (id) => ({ ...structuredClone(STORED), id }) as unknown as Session,
  );
  vi.mocked(sessionsApi.updateSession).mockResolvedValue({ ok: true });
  await useSessionStore.getState().switchSession('old');
  ({ unmount } = renderHook(() => useSessionPersistence()));
});

afterEach(() => {
  unmount();
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('saves of an opened session', () => {
  it('send nothing while it is untouched: not after loading, not from the loop', async () => {
    await tick(90_000);
    expect(savesOf('old')).toHaveLength(0);
  });

  it('would send back exactly what was loaded', () => {
    expect(content(sessionSaveBody('old')!)).toEqual(content(STORED));
  });

  it('send nothing when switching away from it untouched', async () => {
    await useSessionStore.getState().switchSession('other');
    await tick(3_000);
    expect(savesOf('old')).toHaveLength(0);
    expect(savesOf('other')).toHaveLength(0);
  });

  it('skip the unload beacon while the server holds it', () => {
    window.dispatchEvent(new Event('beforeunload'));
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it('send a new message on the debounced save', async () => {
    getChatStore('old').getState().addMessage({ role: 'user', content: 'and tags?', timestamp: 't4' });
    await tick(3_000);

    const saves = savesOf('old');
    expect(saves).toHaveLength(1);
    const sent = saves[0].chatHistory ?? [];
    expect(sent[sent.length - 1].content).toBe('and tags?');
  });

  it('carry an edit that keeps the message count to the next tick, once', async () => {
    answerQuestion();
    await tick(30_000);

    const saves = savesOf('old');
    expect(saves).toHaveLength(1);
    expect(saves[0].chatHistory?.[1].userQuestion?.answered).toBe(true);

    await tick(60_000);
    expect(savesOf('old')).toHaveLength(1);
  });

  it('send a failed save again on the next tick', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(sessionsApi.updateSession).mockRejectedValueOnce(new Error('server down'));
    answerQuestion();

    await tick(60_000);

    expect(savesOf('old')).toHaveLength(2);
    expect(warn).toHaveBeenCalledWith('Failed to save session:', expect.any(Error));
  });

  it('flush an unacknowledged edit through the beacon with the saveSession body', async () => {
    answerQuestion();
    window.dispatchEvent(new Event('beforeunload'));

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, blob] = sendBeacon.mock.calls[0];
    expect(url).toBe('/api/sessions/old/beacon');
    const body = JSON.parse(await blob.text());
    expect(body.chatHistory[1].userQuestion.answered).toBe(true);
    expect(content(body)).toEqual(content(sessionSaveBody('old')!));
  });

  it('never store live-only chunk ids or translation markers', async () => {
    const transcript = getTranscriptionStore('old');
    transcript.setState({
      segments: transcript.getState().segments.map((s) => ({
        ...s, chunks: [{ id: 1, start: 0 }], pendingTranslations: [2],
      })),
    });
    await tick(30_000);

    // Live-only fields are the only difference, so the save matches what is stored.
    const saves = savesOf('old');
    expect(saves).toHaveLength(1);
    expect(content(saves[0])).toEqual(content(STORED));
  });
});
