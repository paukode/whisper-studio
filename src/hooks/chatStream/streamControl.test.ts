import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  killSessionStream,
  registerStreamController,
  wasKillFinalized,
} from './streamControl';

// The ESC kill switch must also stop background shell tasks the session
// spawned — fire-and-forget, never blocking or breaking the synchronous kill.
describe('killSessionStream — background-task stop wire', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fires POST /api/workspace/shell/tasks/stop with the session id', () => {
    killSessionStream('sess-123');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/workspace/shell/tasks/stop',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ session_id: 'sess-123' }),
      }),
    );
  });

  it('does not fire for a null session', () => {
    killSessionStream(null);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('stays synchronous and quiet when the request fails', () => {
    fetchMock.mockRejectedValue(new Error('offline'));
    expect(() => killSessionStream('sess-123')).not.toThrow();
  });
});

// The ESC kill switch must reach any controller currently registered for the
// session — including an auto-approved continuation leg, which now always
// registers its own controller (regression guard for the "continuation
// unstoppable after the outer stream ended" bug).
describe('killSessionStream — aborts the registered controller', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('aborts and kill-finalizes a controller registered for the session', () => {
    const controller = new AbortController();
    registerStreamController('sess-abrt', controller);
    expect(controller.signal.aborted).toBe(false);

    killSessionStream('sess-abrt');

    expect(controller.signal.aborted).toBe(true);
    expect(wasKillFinalized(controller)).toBe(true);
  });
});
