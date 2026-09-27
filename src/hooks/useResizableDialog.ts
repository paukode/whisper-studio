import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Makes a centered modal dialog resizable (edges + corners) and movable (drag a
 * header), persisting its size and position to localStorage so it reopens where
 * the user left it. Position is a translate offset from the centered origin, so
 * the dialog stays centered by default and the CSS flex-centering still applies.
 */
export interface DialogGeometry {
  w: number;
  /** null = natural height (auto) until the user resizes vertically. */
  h: number | null;
  x: number;
  y: number;
}

/** The edges a resize handle moves; a corner names two. */
export interface ResizeEdges {
  n?: boolean;
  e?: boolean;
  s?: boolean;
  w?: boolean;
}

interface Options {
  minW?: number;
  minH?: number;
  defaultW?: number;
  margin?: number;
}

function loadGeometry(key: string, defaultW: number): DialogGeometry {
  try {
    const raw = localStorage.getItem(key);
    if (raw) {
      const g = JSON.parse(raw) as Partial<DialogGeometry>;
      if (typeof g.w === 'number' && typeof g.x === 'number' && typeof g.y === 'number') {
        return { w: g.w, h: typeof g.h === 'number' ? g.h : null, x: g.x, y: g.y };
      }
    }
  } catch {
    /* corrupt or unavailable storage — fall through to default */
  }
  return { w: defaultW, h: null, x: 0, y: 0 };
}

export function useResizableDialog(key: string, opts: Options = {}) {
  const minW = opts.minW ?? 320;
  const minH = opts.minH ?? 260;
  const defaultW = opts.defaultW ?? 480;
  const margin = opts.margin ?? 16;

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [geo, setGeo] = useState<DialogGeometry>(() => loadGeometry(key, defaultW));

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(geo));
    } catch {
      /* ignore persistence failures */
    }
  }, [key, geo]);

  const clamp = useCallback(
    (g: DialogGeometry): DialogGeometry => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const maxW = Math.max(minW, vw - margin * 2);
      const maxH = Math.max(minH, vh - margin * 2);
      const w = Math.min(maxW, Math.max(minW, g.w));
      const h = g.h == null ? null : Math.min(maxH, Math.max(minH, g.h));
      // The offset keeps the whole dialog inside the margins at its NEW size;
      // the rendered size lags one frame behind a resize.
      const dh = h ?? dialogRef.current?.offsetHeight ?? 0;
      const maxX = Math.max(0, (vw - margin * 2 - w) / 2);
      const maxY = Math.max(0, (vh - margin * 2 - dh) / 2);
      const x = Math.max(-maxX, Math.min(maxX, g.x));
      const y = Math.max(-maxY, Math.min(maxY, g.y));
      return { w, h, x, y };
    },
    [minW, minH, margin],
  );

  const drag = useCallback((onMove: (dx: number, dy: number) => void) => {
    return (e: React.PointerEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const sx = e.clientX;
      const sy = e.clientY;
      const move = (ev: PointerEvent) => onMove(ev.clientX - sx, ev.clientY - sy);
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    };
  }, []);

  const onMoveStart = useCallback(
    (e: React.PointerEvent) => {
      const x0 = geo.x;
      const y0 = geo.y;
      drag((dx, dy) => setGeo((g) => clamp({ ...g, x: x0 + dx, y: y0 + dy })))(e);
    },
    [drag, clamp, geo.x, geo.y],
  );

  // The dialog is centered, so a size change alone would grow it on both sides
  // and leave the dragged edge half a step behind the pointer. Shifting the
  // offset by half the change keeps the opposite edge where it was, as a window
  // resizes. Past a viewport margin the clamp takes the offset back, and the
  // dialog grows the other way instead.
  const onResizeStart = useCallback(
    (dir: ResizeEdges) => (e: React.PointerEvent) => {
      const el = dialogRef.current;
      const w0 = el?.offsetWidth || geo.w;
      const h0 = el?.offsetHeight || geo.h || minH;
      const x0 = geo.x;
      const y0 = geo.y;
      drag((dx, dy) =>
        setGeo((g) => {
          const w = dir.e ? w0 + dx : dir.w ? w0 - dx : g.w;
          const h = dir.s ? h0 + dy : dir.n ? h0 - dy : g.h;
          const next = clamp({ ...g, w, h });
          const x = dir.e || dir.w ? x0 + ((next.w - w0) / 2) * (dir.e ? 1 : -1) : g.x;
          const y =
            (dir.s || dir.n) && next.h != null ? y0 + ((next.h - h0) / 2) * (dir.s ? 1 : -1) : g.y;
          return clamp({ ...next, x, y });
        }),
      )(e);
    },
    [drag, clamp, minH, geo.w, geo.h, geo.x, geo.y],
  );

  const reset = useCallback(() => {
    setGeo({ w: defaultW, h: null, x: 0, y: 0 });
  }, [defaultW]);

  // Keep the dialog on-screen if the window shrinks.
  useEffect(() => {
    const onResize = () => setGeo((g) => clamp(g));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [clamp]);

  // `translate`, not `transform`: an entrance animation on `transform` would
  // otherwise override the offset until it ends, then jump.
  const style: React.CSSProperties = {
    width: geo.w,
    height: geo.h ?? undefined,
    translate: geo.x || geo.y ? `${geo.x}px ${geo.y}px` : undefined,
  };

  // Whether the height is pinned by the user rather than following the content.
  // Only then is there spare vertical space for a dialog's inner lists to claim
  // (see the .is-sized rules) — at natural height there is nothing to fill.
  return { dialogRef, style, onMoveStart, onResizeStart, reset, sized: geo.h != null };
}
