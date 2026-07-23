import React, { useEffect, useRef } from 'react';
import { useDockStore, type DockKind } from '@/stores/dockStore';
import { suppressEmbeddedPointerEvents } from '@/utils/dragGuards';
import { LiveBrowserPanel } from './LiveBrowserPanel';
import { PlanPanel } from './PlanPanel';
import { FilePanel } from './FilePanel';
import { TasksPanel } from '@/components/tasks/TasksPanel';

const MIN_FRAC = 0.12;

const KIND_COLOR: Record<DockKind, string> = {
  live: 'var(--accent-live, #e5484d)',
  plan: 'var(--text-warning, #d08b00)',
  tasks: 'var(--text-success, #2a9d5c)',
  file: 'var(--text-secondary, #888)',
};

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

/**
 * RightDock — the dynamic right-side dock. Renders dockStore.panels as a
 * vertical stack, each panel sized by dockStore.sizes[i] (flex-grow), with a
 * draggable handle between neighbours that reallocates space between the two.
 * Panel body is switched on kind. The dock's outer width is owned by the
 * chat|dock Splitter in AppShell.
 */
export const RightDock: React.FC = () => {
  const panels = useDockStore((s) => s.panels);
  const sizes = useDockStore((s) => s.sizes);
  const setSizes = useDockStore((s) => s.setSizes);
  const closePanel = useDockStore((s) => s.closePanel);
  const closeLive = useDockStore((s) => s.closeLive);
  // Remounts the live browser onto a new target (new server or a routed
  // localhost link) so its nav state starts fresh — see dockStore.liveNavKey.
  const liveNavKey = useDockStore((s) => s.liveNavKey);
  const containerRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ i: number; y: number; s0: number; s1: number; h: number } | null>(null);
  const cleanupDragRef = useRef<(() => void) | null>(null);

  // Unmounting mid-drag must release the embed guard and document listeners.
  useEffect(() => () => cleanupDragRef.current?.(), []);

  const onHandleDown = (i: number) => (e: React.MouseEvent) => {
    e.preventDefault();
    const h = containerRef.current?.clientHeight ?? 1;
    drag.current = { i, y: e.clientY, s0: sizes[i] ?? 0.5, s1: sizes[i + 1] ?? 0.5, h };
    const restoreEmbeds = suppressEmbeddedPointerEvents();
    const onMove = (ev: MouseEvent) => {
      if (!drag.current) return;
      // Lost mouseup (released outside the window): end the drag instead of
      // sticking to the cursor.
      if (ev.buttons === 0) { onUp(); return; }
      const { i: idx, y, s0, s1, h: height } = drag.current;
      const f = (ev.clientY - y) / height;
      const ni = Math.max(MIN_FRAC, Math.min(s0 + s1 - MIN_FRAC, s0 + f));
      const next = [...useDockStore.getState().sizes];
      next[idx] = ni;
      next[idx + 1] = s0 + s1 - ni;
      setSizes(next);
    };
    const onUp = () => {
      drag.current = null;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
      restoreEmbeds();
      cleanupDragRef.current = null;
    };
    cleanupDragRef.current = onUp;
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  };

  return (
    <div
      ref={containerRef}
      style={{
        display: 'flex', flexDirection: 'column', flex: '1 1 auto',
        minWidth: 0, minHeight: 0,
        background: 'var(--bg-primary, #1a1a1a)',
        borderLeft: '1px solid var(--border, #333)',
      }}
    >
      {panels.map((p, i) => (
        <React.Fragment key={p.id}>
          <div style={{ display: 'flex', flexDirection: 'column', flexGrow: sizes[i] ?? 1, flexBasis: 0, minHeight: 0, overflow: 'hidden' }}>
            <div
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 10px', flex: '0 0 auto',
                borderBottom: '1px solid var(--border, #333)',
                background: 'var(--bg-secondary, #222)',
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: 999, background: KIND_COLOR[p.kind], flex: '0 0 auto' }} />
              <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-primary, #eee)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {p.title}
              </span>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                onClick={() => (p.kind === 'live' ? closeLive() : closePanel(p.id))}
                aria-label={`Close ${p.title}`}
                title="Close"
                style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--text-muted, #999)', display: 'flex', padding: 2 }}
              >
                <CloseIcon />
              </button>
            </div>
            {p.kind === 'live' && (
              <LiveBrowserPanel
                key={`live-${liveNavKey}`}
                name={String(p.meta?.name ?? p.id)}
                url={(p.meta?.url as string | null | undefined) ?? null}
                port={(p.meta?.port as number | null | undefined) ?? null}
              />
            )}
            {p.kind === 'tasks' && <TasksPanel />}
            {p.kind === 'plan' && <PlanPanel planId={String(p.meta?.planId ?? p.id)} />}
            {p.kind === 'file' && (
              <FilePanel
                path={String(p.meta?.path ?? p.id)}
                startLine={p.meta?.startLine as number | undefined}
                endLine={p.meta?.endLine as number | undefined}
                lineRev={p.meta?.lineRev as number | undefined}
              />
            )}
          </div>
          {i < panels.length - 1 && (
            <div
              onMouseDown={onHandleDown(i)}
              style={{ height: 6, cursor: 'row-resize', background: 'var(--border-strong, #444)', flex: '0 0 auto' }}
            />
          )}
        </React.Fragment>
      ))}
    </div>
  );
};

export default RightDock;
