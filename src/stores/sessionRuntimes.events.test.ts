/**
 * The app-wide event channel (/api/sessions/events) keeps the one MCP server
 * list live: every (re)open reloads it, and a backend `mcp_changed` frame
 * refetches it. The channel is app-scoped, so dropping the last session
 * runtime must not close it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }
  emit(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

async function load() {
  vi.resetModules();
  const runtimes = await import('./sessionRuntimes');
  const { useMcpStore } = await import('./mcpStore');
  const refresh = vi.fn().mockResolvedValue(undefined);
  const applyChange = vi.fn();
  useMcpStore.setState({ refresh, applyChange });
  return { runtimes, refresh, applyChange };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => vi.unstubAllGlobals());

describe('app event channel', () => {
  it('opens one connection however often it is asked', async () => {
    const { runtimes } = await load();
    runtimes.openEventChannel();
    runtimes.openEventChannel();
    runtimes.getRuntime('sess-1');
    expect(FakeEventSource.instances.map((es) => es.url)).toEqual(['/api/sessions/events']);
  });

  it('reloads the MCP list on every open, the first included', async () => {
    const { runtimes, refresh } = await load();
    runtimes.openEventChannel();
    FakeEventSource.instances[0].open();
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    // EventSource's own reconnect fires onopen again.
    FakeEventSource.instances[0].open();
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(2));
  });

  it('hands an mcp_changed frame to the MCP store with its revision', async () => {
    const { runtimes, applyChange } = await load();
    runtimes.openEventChannel();
    FakeEventSource.instances[0].emit({ session_id: '', mcp_changed: { revision: 7 } });
    await vi.waitFor(() => expect(applyChange).toHaveBeenCalledWith(7));
  });

  it('stays open when the last session runtime is dropped', async () => {
    const { runtimes } = await load();
    runtimes.openEventChannel();
    runtimes.getRuntime('sess-1');
    runtimes.dropRuntime('sess-1');
    expect(FakeEventSource.instances[0].closed).toBe(false);
  });
});
