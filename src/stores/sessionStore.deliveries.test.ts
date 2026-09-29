/**
 * A reply's verified deliveries are part of the conversation: a save sends
 * them on the message and a reload shows them there again. The client saves
 * and loads messages as they are, and the server stores chatHistory verbatim
 * (server/infrastructure/sessions.py, _upsert_session and _row_to_dict), so
 * no side strips the field: saves and loads strip the same nothing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/types/session';
import type { Delivery } from '@/types/chat';

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
import { sessionSaveBody, useSessionStore } from './sessionStore';
import { dropRuntime, getChatStore, useRuntimeIndex } from './sessionRuntimes';

const REPORT: Delivery = {
  kind: 'file',
  target: '/Users/me/Downloads/report.html',
  label: 'report.html',
  detail: '12.4 KB, saved 19:31',
  href: '#wsfile=%2FUsers%2Fme%2FDownloads%2Freport.html&open=os',
  at: '2026-09-29T19:31:02+00:00',
};
const PUSH: Delivery = { kind: 'push', target: 'main', label: 'main', detail: 'to origin/main' };

/** GET /api/sessions/{id} for a session the server holds as `body` was
 *  saved: the JSON that went over the wire, with updatedAt served as date. */
function servedAsSaved(body: Session): Session {
  const { updatedAt, ...rest } = JSON.parse(JSON.stringify(body)) as Session;
  return { ...rest, date: updatedAt } as unknown as Session;
}

/** Open ``id`` from the server copy, as a restart or a reopened tab does. */
async function reopen(id: string) {
  dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null });
  await useSessionStore.getState().switchSession(id);
  return getChatStore(id).getState().messages;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(sessionsApi.createSession).mockResolvedValue({ ok: true });
  vi.mocked(sessionsApi.updateSession).mockResolvedValue({ ok: true });
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('verified deliveries across a save and a reload', () => {
  it('stay on the reply that claimed them', async () => {
    const id = useSessionStore.getState().createSession();
    const chat = getChatStore(id).getState();
    chat.addMessage({ role: 'user', content: 'write the report and push it', timestamp: 't1' });
    chat.setStreaming(true);
    chat.addLiveDeliveries([REPORT]);
    chat.addLiveDeliveries([PUSH]);
    chat.finishStream({ role: 'assistant', content: 'Saved report.html and pushed main.', timestamp: 't2' });

    const saved = sessionSaveBody(id)!;
    expect(saved.chatHistory[1].deliveries).toEqual([REPORT, PUSH]);

    vi.mocked(sessionsApi.getSession).mockResolvedValue(servedAsSaved(saved));
    const reloaded = await reopen(id);

    expect(reloaded.map((m) => m.content)).toEqual(saved.chatHistory.map((m) => m.content));
    expect(reloaded[1].deliveries).toEqual([REPORT, PUSH]);
  });

  it('go back out exactly as they were loaded', async () => {
    const chatHistory = [
      { role: 'user', content: 'push it', timestamp: '2026-09-29T19:30:00.000Z' },
      {
        role: 'assistant',
        content: 'Pushed main and saved the report.',
        timestamp: '2026-09-29T19:31:05.000Z',
        deliveries: [PUSH, REPORT],
      },
    ];
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      id: 'old',
      title: 'Push and report',
      customTitle: false,
      generatedTitle: true,
      createdAt: '2026-09-29T19:29:00.000Z',
      date: '2026-09-29T19:31:05.000Z',
      chatHistory,
      segments: [],
      speakerNames: {},
    } as unknown as Session);

    await useSessionStore.getState().switchSession('old');

    expect(getChatStore('old').getState().messages[1].deliveries).toEqual([PUSH, REPORT]);
    expect(JSON.parse(JSON.stringify(sessionSaveBody('old')!.chatHistory))).toEqual(chatHistory);
  });
});
