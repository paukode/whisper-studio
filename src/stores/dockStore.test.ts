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
