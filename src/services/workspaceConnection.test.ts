/**
 * Disconnect is instant, single-flight, ordered before a reconnect, and never
 * silent about a failure. See the module docstring for why each rule exists.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useUIStore } from '@/stores/uiStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import {
  disconnectWorkspace,
  settleWorkspaceOps,
  syncWorkspaceStatus,
} from './workspaceConnection';

type Pending = { url: string; method: string; resolve: (r: unknown) => void; reject: (e: unknown) => void };
let pending: Pending[] = [];

const fetchMock = vi.fn((url: string, init?: RequestInit) => {
  return new Promise((resolve, reject) => {
    pending.push({ url: String(url), method: init?.method ?? 'GET', resolve, reject });
  });
});

const json = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: '',
  headers: new Headers({ 'content-type': 'application/json' }),
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

const posts = () => pending.filter((p) => p.url.includes('/api/workspace/disconnect'));
const take = (fragment: string) => {
  const i = pending.findIndex((p) => p.url.includes(fragment));
  expect(i, `no pending request for ${fragment}`).toBeGreaterThanOrEqual(0);
  return pending.splice(i, 1)[0];
};

const addToast = vi.fn();

beforeEach(async () => {
  // A previous test's disconnect must not be left in flight.
  for (const p of pending) p.resolve(json({ disconnected: true, warnings: [] }));
  await settleWorkspaceOps();
  pending = [];
  fetchMock.mockClear();
  addToast.mockClear();
  vi.stubGlobal('fetch', fetchMock);
  useUIStore.setState({ wsConnected: true, wsPath: '/repo', addToast } as never);
  useWorkspaceStore.setState({ editorTabs: [], activeTabPath: null });
});

describe('disconnectWorkspace', () => {
  it('updates the UI on the click, before the server has answered', async () => {
    void disconnectWorkspace();
    expect(useUIStore.getState().wsConnected).toBe(false);
    expect(useUIStore.getState().wsPath).toBe('');
    await flush();
    expect(posts()).toHaveLength(1);
  });

  it('sends one request however many times it is clicked', async () => {
    const first = disconnectWorkspace();
    const second = disconnectWorkspace();
    const third = disconnectWorkspace();
    await flush();
    expect(second).toBe(first);
    expect(third).toBe(first);
    expect(posts()).toHaveLength(1);
    take('/api/workspace/disconnect').resolve(json({ disconnected: true, warnings: [] }));
    await first;
    expect(addToast).not.toHaveBeenCalled();
  });

  it('holds a reconnect until the disconnect has reached the server', async () => {
    const done = disconnectWorkspace();
    let settled = false;
    void settleWorkspaceOps().then(() => { settled = true; });
    await flush();
    expect(settled).toBe(false);
    take('/api/workspace/disconnect').resolve(json({ disconnected: true, warnings: [] }));
    await done;
    await flush();
    expect(settled).toBe(true);
  });

  it('reports a failed disconnect and re-syncs the UI from the server', async () => {
    const done = disconnectWorkspace();
    await flush();
    take('/api/workspace/disconnect').resolve(
      json({ error: 'Could not save the workspace config, still connected: disk full' }, 500),
    );
    await flush();
    // The failure resyncs from /status, which says the workspace is still there.
    take('/api/workspace/status').resolve(json({ connected: true, path: '/repo' }));
    await done;
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringContaining('disk full') }),
    );
    expect(useUIStore.getState().wsConnected).toBe(true);
    expect(useUIStore.getState().wsPath).toBe('/repo');
  });

  it('gives up on a request that never reached the server, and says so', async () => {
    const timer = new AbortController();
    const timeout = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timer.signal);
    const done = disconnectWorkspace();
    await flush();
    expect(timeout).toHaveBeenCalledWith(10_000);
    const reason = new DOMException('signal timed out', 'TimeoutError');
    timer.abort(reason);
    take('/api/workspace/disconnect').reject(reason);
    await flush();
    take('/api/workspace/status').reject(new TypeError('Failed to fetch'));
    await done;
    timeout.mockRestore();
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'error',
        message: expect.stringContaining('did not answer within 10 s'),
      }),
    );
    // The server could not be asked; the optimistic state stays until the
    // next resync can reach it.
    expect(useUIStore.getState().wsConnected).toBe(false);
  });

  it('shows the server-side teardown warnings', async () => {
    const done = disconnectWorkspace();
    await flush();
    take('/api/workspace/disconnect').resolve(
      json({ disconnected: true, warnings: ['Could not reset the git watcher: boom'] }),
    );
    await done;
    expect(addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'warning', message: expect.stringContaining('git watcher') }),
    );
    expect(useUIStore.getState().wsConnected).toBe(false);
  });

  it('closes clean editor tabs and keeps unsaved ones', async () => {
    useWorkspaceStore.setState({
      editorTabs: [
        { path: 'a.ts', language: 'ts', content: 'a', originalContent: 'a', isDirty: false, root: '/repo' },
        { path: 'b.ts', language: 'ts', content: 'b2', originalContent: 'b', isDirty: true, root: '/repo' },
      ],
      activeTabPath: 'a.ts',
    });
    void disconnectWorkspace();
    const { editorTabs, activeTabPath } = useWorkspaceStore.getState();
    expect(editorTabs.map((t) => t.path)).toEqual(['b.ts']);
    expect(activeTabPath).toBe('b.ts');
  });
});

describe('an unsaved tab kept across a disconnect', () => {
  it('refuses to save into a different workspace', async () => {
    useWorkspaceStore.setState({
      editorTabs: [
        { path: 'b.ts', language: 'ts', content: 'b2', originalContent: 'b', isDirty: true, root: '/repo' },
      ],
      activeTabPath: 'b.ts',
    });
    useUIStore.getState().setWsConnected(true, '/other');
    await expect(useWorkspaceStore.getState().saveTab('b.ts')).rejects.toThrow(/belongs to \/repo/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('syncWorkspaceStatus', () => {
  it('drops an answer that was in flight when the user disconnected', async () => {
    // Focus fires on the mousedown of the click: the GET leaves first.
    const sync = syncWorkspaceStatus();
    await flush();
    const done = disconnectWorkspace();
    await flush();
    // The server answered the GET before it saw the POST.
    take('/api/workspace/status').resolve(json({ connected: true, path: '/repo' }));
    take('/api/workspace/disconnect').resolve(json({ disconnected: true, warnings: [] }));
    await expect(sync).resolves.toBe('stale');
    await done;
    expect(useUIStore.getState().wsConnected).toBe(false);
  });

  it('drops an answer that a connect overtook', async () => {
    useUIStore.setState({ wsConnected: false, wsPath: '' });
    const sync = syncWorkspaceStatus();
    await flush();
    useUIStore.getState().setWsConnected(true, '/new');
    take('/api/workspace/status').resolve(json({ connected: false }));
    await expect(sync).resolves.toBe('stale');
    expect(useUIStore.getState().wsPath).toBe('/new');
  });

  it('applies a current answer in both directions', async () => {
    const sync = syncWorkspaceStatus();
    await flush();
    take('/api/workspace/status').resolve(json({ connected: false }));
    await expect(sync).resolves.toBe('applied');
    expect(useUIStore.getState().wsConnected).toBe(false);

    const again = syncWorkspaceStatus();
    await flush();
    take('/api/workspace/status').resolve(json({ connected: true, path: '/back' }));
    await expect(again).resolves.toBe('applied');
    expect(useUIStore.getState().wsPath).toBe('/back');
  });
});
