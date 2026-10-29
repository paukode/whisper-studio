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
  const { useBackgroundTaskStore } = await import('./backgroundTaskStore');
  const refresh = vi.fn().mockResolvedValue(undefined);
  const applyChange = vi.fn();
  const hydrate = vi.fn().mockResolvedValue(undefined);
  const applyEvent = vi.fn();
  useMcpStore.setState({ refresh, applyChange });
  useBackgroundTaskStore.setState({ hydrate, applyEvent });
  return { runtimes, refresh, applyChange, hydrate, applyEvent };
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

  it('reloads the task list on every open and routes task events to it without a session runtime', async () => {
    const { runtimes, hydrate, applyEvent } = await load();
    runtimes.openEventChannel();
    FakeEventSource.instances[0].open();
    await vi.waitFor(() => expect(hydrate).toHaveBeenCalledTimes(1));
    const payload = {
      event_type: 'task_started', task_id: 't1', kind: 'shell', title: 'sleep 9', status: 'running',
      timestamp: '2026-10-18T10:00:00Z',
    };
    // No runtime exists for this session: the list is global and still updates.
    FakeEventSource.instances[0].emit({ session_id: 'never-opened', task_event: payload });
    await vi.waitFor(() => expect(applyEvent).toHaveBeenCalledWith('never-opened', payload));
  });

  it('stays open when the last session runtime is dropped', async () => {
    const { runtimes } = await load();
    runtimes.openEventChannel();
    runtimes.getRuntime('sess-1');
    runtimes.dropRuntime('sess-1');
    expect(FakeEventSource.instances[0].closed).toBe(false);
  });
});

describe('workspace changes on the event channel', () => {
  it('turns a workspace_changed frame into the workspace-refresh event, naming its folders and files', async () => {
    const { runtimes } = await load();
    runtimes.openEventChannel();
    const seen: unknown[] = [];
    const onRefresh = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('whisper-workspace-refresh', onRefresh);
    try {
      FakeEventSource.instances[0].emit({
        session_id: '',
        workspace_changed: { root: '/w', dirs: ['', 'app'], files: ['app/main.py'] },
      });
    } finally {
      window.removeEventListener('whisper-workspace-refresh', onRefresh);
    }
    expect(seen).toEqual([{ dirs: ['', 'app'], files: ['app/main.py'] }]);
  });
});

describe('background commands and the terminal on the event channel', () => {
  it('adds a finished command as a task_report row and asks for a wake', async () => {
    const requestWake = vi.fn();
    vi.doMock('@/hooks/chatStream/backgroundWake', () => ({ requestWake, flushPendingWakes: vi.fn() }));
    try {
      const { runtimes } = await load();
      runtimes.openEventChannel();
      const chat = runtimes.getChatStore('sess-w');
      const report = { task_id: 't1', command: 'make', status: 'completed', timestamp: '2026-10-28T17:00:00Z' };
      FakeEventSource.instances[0].emit({ session_id: 'sess-w', task_report: report });
      FakeEventSource.instances[0].emit({ session_id: 'sess-w', task_wake: { task_ids: ['t1'] } });
      expect(chat.getState().messages).toEqual([
        { role: 'task_report', content: '', timestamp: report.timestamp, taskReport: report },
      ]);
      await vi.waitFor(() => expect(requestWake).toHaveBeenCalledWith('sess-w', ['t1']));
    } finally {
      vi.doUnmock('@/hooks/chatStream/backgroundWake');
    }
  });

  it('asks the UI to show the terminal a command runs in, with no session runtime', async () => {
    const { runtimes } = await load();
    const { useUIStore } = await import('./uiStore');
    runtimes.openEventChannel();
    FakeEventSource.instances[0].emit({
      session_id: '',
      terminal_show: { terminal_id: 'p1', cwd: '/w', opened: true },
    });
    await vi.waitFor(() =>
      expect(useUIStore.getState().terminalShow).toMatchObject({ terminalId: 'p1', cwd: '/w' }),
    );
  });
});
