/**
 * Hung-up voice conversations keep draining their delegated work in the
 * background, one socket per conversation, each bound to the chat session it
 * started in. Stop and ESC in one session must reach only that session's
 * sockets: the old cancelRuns() signalled every draining socket, so pressing
 * Stop in session B cancelled the work session A was still finishing.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { voiceController } from './voiceController';
import { useVoiceStore } from '@/stores/voiceStore';

interface FakeSocket {
  readyState: number;
  send: ReturnType<typeof vi.fn>;
}

/** The controller's per-socket bookkeeping, seeded the way onDraining leaves
 *  it after a hang-up with delegated runs still going. */
interface DrainingState {
  draining: Set<FakeSocket>;
  socketSession: Map<FakeSocket, string>;
  socketRuns: Map<FakeSocket, Set<string>>;
}

function drain(sessionId: string, runIds: string[] = []): FakeSocket {
  const ws: FakeSocket = { readyState: WebSocket.OPEN, send: vi.fn() };
  const internals = voiceController as unknown as DrainingState;
  internals.draining.add(ws);
  internals.socketSession.set(ws, sessionId);
  internals.socketRuns.set(ws, new Set(runIds));
  return ws;
}

describe('voiceController draining sockets are per session', () => {
  beforeEach(() => {
    const internals = voiceController as unknown as DrainingState;
    internals.draining.clear();
    internals.socketSession.clear();
    internals.socketRuns.clear();
    useVoiceStore.getState().reset();
  });

  it('cancelRuns signals only the given session', () => {
    const a = drain('sess-A');
    const b = drain('sess-B');

    voiceController.cancelRuns('sess-B');

    expect(b.send).toHaveBeenCalledWith(JSON.stringify({ type: 'cancel' }));
    expect(a.send).not.toHaveBeenCalled();
  });

  it('a session with no draining conversation cancels nothing', () => {
    const a = drain('sess-A');
    voiceController.cancelRuns('sess-C');
    voiceController.cancelRuns(null);
    expect(a.send).not.toHaveBeenCalled();
  });

  it('reports draining work per session', () => {
    drain('sess-A');
    expect(voiceController.hasDrainingRuns('sess-A')).toBe(true);
    expect(voiceController.hasDrainingRuns('sess-B')).toBe(false);
    expect(voiceController.hasDrainingRuns(null)).toBe(false);
  });

  it('a delegated run belongs to the session of the call that started it', () => {
    drain('sess-A', ['run-a']);
    drain('sess-B', ['run-b']);
    expect(voiceController.runSession('run-a')).toBe('sess-A');
    expect(voiceController.runSession('run-b')).toBe('sess-B');
    expect(voiceController.runSession('run-gone')).toBeNull();
  });

  it('sendText reports that nothing was sent when no call is connected', () => {
    expect(voiceController.sendText('hello')).toBe(false);
  });
});
