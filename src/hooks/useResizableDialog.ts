import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Makes a centered modal dialog resizable (corner + edges) and movable (drag a
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
      const el = dialogRef.current;
      const dw = el?.offsetWidth ?? w;
      const dh = el?.offsetHeight ?? h ?? 0;
      const maxX = Math.max(0, (vw - dw) / 2);
      const maxY = Math.max(0, (vh - dh) / 2);
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

  const onResizeStart = useCallback(
    (dir: { e?: boolean; s?: boolean }) => (e: React.PointerEvent) => {
      const el = dialogRef.current;
      const w0 = el?.offsetWidth ?? geo.w;
      const h0 = el?.offsetHeight ?? geo.h ?? minH;
      drag((dx, dy) =>
        setGeo((g) => clamp({ ...g, w: dir.e ? w0 + dx : g.w, h: dir.s ? h0 + dy : g.h })),
      )(e);
    },
    [drag, clamp, minH, geo.w, geo.h],
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

  const style: React.CSSProperties = {
    width: geo.w,
    height: geo.h ?? undefined,
    transform: geo.x || geo.y ? `translate(${geo.x}px, ${geo.y}px)` : undefined,
  };

  return { dialogRef, style, onMoveStart, onResizeStart, reset };
}
