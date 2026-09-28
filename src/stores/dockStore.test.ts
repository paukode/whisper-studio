import { describe, it, expect, beforeEach } from 'vitest';
import { useDockStore } from './dockStore';

beforeEach(() => {
  useDockStore.setState({ panels: [], sizes: [], open: false });
});

describe('dockStore.openFile', () => {
  it('opens one panel per path and updates the line target on re-click', () => {
    const s = useDockStore.getState();
    s.openFile({ path: '/w/a.md', title: 'a.md', startLine: 3, endLine: 9 });
    let panels = useDockStore.getState().panels;
    expect(panels).toHaveLength(1);
    expect(panels[0].id).toBe('file:/w/a.md');
    expect(panels[0].meta).toMatchObject({ startLine: 3, endLine: 9, lineRev: 1 });

    // Re-click the same file at a different range: same panel, new target, bumped rev.
    useDockStore.getState().openFile({ path: '/w/a.md', title: 'a.md', startLine: 20, endLine: 25 });
    panels = useDockStore.getState().panels;
    expect(panels).toHaveLength(1);
    expect(panels[0].meta).toMatchObject({ startLine: 20, endLine: 25, lineRev: 2 });
    expect(useDockStore.getState().open).toBe(true);
  });

  it('opens distinct panels for distinct paths', () => {
    const s = useDockStore.getState();
    s.openFile({ path: '/w/a.md', title: 'a.md' });
    s.openFile({ path: '/w/b.md', title: 'b.md' });
    const { panels, sizes } = useDockStore.getState();
    expect(panels.map((p) => p.id)).toEqual(['file:/w/a.md', 'file:/w/b.md']);
    expect(sizes).toHaveLength(2);
  });
});

describe('dockStore.setLiveSession', () => {
  beforeEach(() => {
    useDockStore.setState({ liveSession: null, liveNavUrl: null, liveNavKey: 0, liveDismissed: null });
  });

  it("treats the same name owned by another chat as a different server", () => {
    const s = useDockStore.getState();
    s.setLiveSession({ name: 'dev', url: 'http://localhost:5173', port: 5173, owner: 'chat-a' });
    useDockStore.getState().previewUrl('http://localhost:5173/admin');
    const keyBefore = useDockStore.getState().liveNavKey;

    // Chat A stopped "dev"; chat B started its own "dev" and is now on screen.
    useDockStore
      .getState()
      .setLiveSession({ name: 'dev', url: 'http://localhost:5173', port: 5173, owner: 'chat-b' });

    const after = useDockStore.getState();
    expect(after.liveNavUrl).toBeNull();
    expect(after.liveNavKey).toBeGreaterThan(keyBefore);
    expect(after.panels.find((p) => p.kind === 'live')?.meta?.owner).toBe('chat-b');
  });

  it('a url refresh of the same server keeps the routed URL', () => {
    const s = useDockStore.getState();
    s.setLiveSession({ name: 'dev', url: null, port: 5173, owner: 'chat-a' });
    useDockStore.getState().previewUrl('http://localhost:5173/admin');
    useDockStore
      .getState()
      .setLiveSession({ name: 'dev', url: 'http://localhost:5173', port: 5173, owner: 'chat-a' });
    expect(useDockStore.getState().liveNavUrl).toBe('http://localhost:5173/admin');
  });
});
