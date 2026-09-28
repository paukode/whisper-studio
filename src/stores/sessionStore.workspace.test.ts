/**
 * A session's sidebar row learns the folder its turn ran in when the turn
 * ends, the same moment the server records it (sessions.workspace_path), so
 * "Open workspace in" appears without reloading the session list. A turn
 * with no folder connected keeps the row's last one, as the server does, and
 * so does a turn whose folder changed mid-stream: the server reads the folder
 * through the turn's workspace latch, which records nothing once the user
 * switches folders, from this chat or another.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
import { dropRuntime, getChatStore, useRuntimeIndex } from './sessionRuntimes';
import { useSessionStore } from './sessionStore';
import { useUIStore } from './uiStore';

function live(id: string): Session {
  return {
    id,
    title: `Session ${id}`,
    customTitle: false,
    generatedTitle: false,
    createdAt: '2026-09-11T19:40:00.000Z',
    updatedAt: '2026-09-11T19:40:00.000Z',
    segments: [],
    chatHistory: [],
    speakerNames: {},
  };
}

function row(id: string, workspacePath: string) {
  return {
    id,
    title: `Session ${id}`,
    date: '2026-09-11T19:40:00.000Z',
    segmentCount: 0,
    chatCount: 0,
    workspacePath,
    pinned: false,
    archived: false,
  };
}

function workspaceOf(id: string): string | undefined {
  return useSessionStore.getState().sessions.find((s) => s.id === id)?.workspacePath;
}

/** A turn in session ``id`` streams and ends, as a chat send drives it. */
async function runTurn(id: string) {
  const chat = getChatStore(id).getState();
  chat.addMessage({ role: 'user', content: 'split the parser', timestamp: 't1' });
  chat.setStreaming(true);
  chat.finishStream({ role: 'assistant', content: 'Done.', timestamp: 't2' });
  // The runtime registry syncs the row shortly after the stream ends.
  await vi.advanceTimersByTimeAsync(300);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(sessionsApi.updateSession).mockResolvedValue({ ok: true });
  useSessionStore.setState({
    currentSessionId: 's1',
    liveSessions: { s1: live('s1'), s2: live('s2') },
    sessions: [row('s1', ''), row('s2', '/Users/me/code/other')],
  });
});

afterEach(() => {
  for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  useSessionStore.setState({ currentSessionId: null, liveSessions: {}, sessions: [] });
  useUIStore.getState().setWsConnected(false);
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("a session's sidebar row after a turn", () => {
  it('carries the folder that stayed connected for the whole turn', async () => {
    useUIStore.getState().setWsConnected(true, '/Users/me/code/parser');

    await runTurn('s1');

    expect(workspaceOf('s1')).toBe('/Users/me/code/parser');
    // Only the session whose turn ended.
    expect(workspaceOf('s2')).toBe('/Users/me/code/other');
  });

  it('keeps its last folder when the turn ran with none connected', async () => {
    useSessionStore.setState({ sessions: [row('s1', '/Users/me/code/parser')] });
    useUIStore.getState().setWsConnected(false);

    await runTurn('s1');

    expect(workspaceOf('s1')).toBe('/Users/me/code/parser');
  });

  it('leaves the row alone when the connected folder changes during the turn', async () => {
    useUIStore.getState().setWsConnected(true, '/Users/me/code/parser');
    const chat = getChatStore('s1').getState();
    chat.addMessage({ role: 'user', content: 'split the parser', timestamp: 't1' });
    chat.setStreaming(true);
    // Another folder connected mid-turn, from this chat or another one.
    useUIStore.getState().setWsConnected(true, '/Users/me/code/lexer');
    chat.finishStream({ role: 'assistant', content: 'Done.', timestamp: 't2' });
    await vi.advanceTimersByTimeAsync(300);

    expect(workspaceOf('s1')).toBe('');
  });

  it("is not stamped with a folder another chat connected while its turn ran", async () => {
    useUIStore.getState().setWsConnected(true, '/Users/me/code/parser');
    const s1 = getChatStore('s1').getState();
    s1.addMessage({ role: 'user', content: 'split the parser', timestamp: 't1' });
    s1.setStreaming(true);
    // The user moves on to s2 and connects its folder there.
    useUIStore.getState().setWsConnected(true, '/Users/me/code/lexer');
    await runTurn('s2');
    s1.finishStream({ role: 'assistant', content: 'Done.', timestamp: 't2' });
    await vi.advanceTimersByTimeAsync(300);

    expect(workspaceOf('s1')).toBe('');
    expect(workspaceOf('s2')).toBe('/Users/me/code/lexer');
  });
});
