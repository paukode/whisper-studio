import React, { useCallback, useEffect, useRef, type ReactNode } from 'react';
import { suppressEmbeddedPointerEvents } from '@/utils/dragGuards';

/**
 * Splitter — a two-pane resizable layout driven by a ratio prop.
 *
 * Renders:
 *   <container flex-direction={row|column}>
 *     <slot1 flex={ratio*N} 1 0>   {children[0]}
 *     <handle flex={0 0 6px}>      drag affordance
 *     <slot2 flex={(1-ratio)*N} 1 0> {children[1]}
 *   </container>
 *
 * Key property: ratio is a *fraction of available space*, not a pixel count.
 * Adding or removing sibling panes outside this Splitter has no effect on
 * the split — flexbox redistribution handles it. The drag handler computes
 * the new ratio from clientX/clientY relative to the container's bounding
 * rect and calls onChange. No DOM mutation happens here.
 *
 * Min sizes are enforced by CSS min-width / min-height on the inner panels;
 * the ratio is additionally clamped to [min, max] to keep state sane.
 */
export interface SplitterProps {
  /** 'horizontal' = panes laid out side-by-side (drag X). 'vertical' = stacked (drag Y). */
  direction: 'horizontal' | 'vertical';
  /** Fraction of available space allocated to children[0]. 0..1. */
  ratio: number;
  /** Called continuously while dragging with the clamped new ratio. */
  onChange: (r: number) => void;
  /** Min allowed ratio (default 0.1). */
  min?: number;
  /** Max allowed ratio (default 0.9). */
  max?: number;
  /** Optional className for the outer container. */
  className?: string;
  /** Optional inline style merged onto the outer container. */
  style?: React.CSSProperties;
  /** Optional id passed to the outer container. */
  id?: string;
  /** Optional additional class on the drag handle for theming. */
  handleClassName?: string;
  /** Minimum size in px for slot 1 (width for horizontal, height for vertical). Default 200. */
  firstMinPx?: number;
  /** Minimum size in px for slot 2 (width for horizontal, height for vertical). Default 280. */
  secondMinPx?: number;
  /** Exactly two children — the two panes. */
  children: [ReactNode, ReactNode];
}

const HANDLE_THICKNESS_PX = 6;

export const Splitter: React.FC<SplitterProps> = ({
  direction,
  ratio,
  onChange,
  min = 0.1,
  max = 0.9,
  className,
  style,
  id,
  handleClassName,
  firstMinPx = 200,
  secondMinPx = 280,
  children,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const restoreEmbedsRef = useRef<(() => void) | null>(null);
  const onChangeRef = useRef(onChange);
  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      draggingRef.current = true;
      // A drag whose mouseup was lost (released outside the window) can leave
      // a pending restore; release it before arming a new one so guards never
      // stack or leak.
      restoreEmbedsRef.current?.();
      restoreEmbedsRef.current = suppressEmbeddedPointerEvents();
      document.body.style.cursor = direction === 'horizontal' ? 'col-resize' : 'row-resize';
      document.body.style.userSelect = 'none';
    },
    [direction],
  );

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current) return;
      // A move with no button held means the mouseup was lost (released outside
      // the window). End the drag rather than let the handle stick to the cursor.
      if (e.buttons === 0) { onUp(); return; }
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const pos = direction === 'horizontal' ? e.clientX - rect.left : e.clientY - rect.top;
      const total = direction === 'horizontal' ? rect.width : rect.height;
      if (total <= 0) return;
      // Account for the handle's own width when mapping cursor → ratio so the
      // pane edge tracks the cursor rather than drifting by half a handle.
      const usable = Math.max(1, total - HANDLE_THICKNESS_PX);
      const adjusted = Math.max(0, Math.min(usable, pos - HANDLE_THICKNESS_PX / 2));
      // Translate px min-sizes into ratio bounds so the cursor can't drag past
      // a point where either pane would render smaller than its allowed min.
      const dynMin = Math.max(min, firstMinPx / usable);
      const dynMax = Math.min(max, 1 - secondMinPx / usable);
      const lo = Math.min(dynMin, dynMax);
      const hi = Math.max(dynMin, dynMax);
      const next = Math.max(lo, Math.min(hi, adjusted / usable));
      onChangeRef.current(next);
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      restoreEmbedsRef.current?.();
      restoreEmbedsRef.current = null;
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      // Unmounting mid-drag (a pane toggled away by a keybinding or async
      // store update) would otherwise leave the embed guard on forever.
      onUp();
    };
  }, [direction, min, max, firstMinPx, secondMinPx]);

  // Integerize grow values for stable diffing and to avoid float noise in
  // the inline style string across renders.
  const safeRatio = Math.max(min, Math.min(max, ratio));
  const grow1 = Math.max(1, Math.round(safeRatio * 1000));
  const grow2 = Math.max(1, 1000 - grow1);

  const isHorizontal = direction === 'horizontal';

  // Wrappers carry the min-size along the split axis so flexbox squeezes the
  // *opposite* pane rather than letting either pane overflow when the ratio
  // would otherwise push it below a usable size. The cross-axis stays 0 so
  // the pane is free to fill height (for horizontal) or width (for vertical).
  const slotBase: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    minWidth: isHorizontal ? undefined : 0,
    minHeight: isHorizontal ? 0 : undefined,
    overflow: 'hidden',
  };
  const slot1Min = isHorizontal ? { minWidth: firstMinPx } : { minHeight: firstMinPx };
  const slot2Min = isHorizontal ? { minWidth: secondMinPx } : { minHeight: secondMinPx };

  return (
    <div
      ref={containerRef}
      id={id}
      className={className}
      style={{
        display: 'flex',
        flexDirection: isHorizontal ? 'row' : 'column',
        flex: '1 1 auto',
        minWidth: 0,
        minHeight: 0,
        overflow: 'hidden',
        ...style,
      }}
    >
      <div style={{ ...slotBase, ...slot1Min, flex: `${grow1} 1 0` }}>{children[0]}</div>
      <div
        className={`resize-handle${handleClassName ? ` ${handleClassName}` : ''}`}
        onMouseDown={onMouseDown}
        style={{
          flex: `0 0 ${HANDLE_THICKNESS_PX}px`,
          cursor: isHorizontal ? 'col-resize' : 'row-resize',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <div
          className="resize-handle-bar"
          style={isHorizontal ? { width: 3, height: '100%' } : { width: '100%', height: 3 }}
        />
      </div>
      <div style={{ ...slotBase, ...slot2Min, flex: `${grow2} 1 0` }}>{children[1]}</div>
    </div>
  );
};
