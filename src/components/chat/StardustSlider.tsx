import React, { useEffect, useRef } from 'react';

/**
 * The shared "star-dust" slider track: a 26px capsule with an orb the full
 * height of the track (flush edge to edge) that rides to the active stop,
 * trailing a colour-shifting comet tail. The groove (--bg-inset) and the orb
 * highlight (--text-primary) are read from the live theme and re-read whenever
 * `colorKey` changes; the per-stop colours come from `readColors`, so a caller
 * can drive it off effort tiers, context-window sizes, or anything else. Honours
 * prefers-reduced-motion (no idle twinkle or drift).
 *
 * `value`/`onChange` speak in STOP INDICES (0..count-1) — the caller maps those
 * to its own domain. Tick labels and descriptions live in the parent popover.
 */
export type RGB = [number, number, number];

export function parseColor(c: string): RGB {
  c = c.trim();
  const m = c.match(/^#?([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (m) {
    let h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const r = c.match(/rgba?\(([^)]+)\)/i);
  if (r) {
    const p = r[1].split(',').map((x) => parseFloat(x));
    return [p[0] || 0, p[1] || 0, p[2] || 0];
  }
  return [150, 150, 150];
}

export function mix(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

interface Props {
  /** Number of stops on the track. */
  count: number;
  /** Active stop, as an index in [0, count). */
  value: number;
  /** Called with the new stop index on drag/click/keyboard. */
  onChange: (index: number) => void;
  /** One RGB per stop, read live (on mount and whenever `colorKey` changes). */
  readColors: () => RGB[];
  /** Change this string to force a palette re-read (e.g. theme + stop list). */
  colorKey: string;
  ariaLabel: string;
  /** Screen-reader text for a stop index (e.g. "High", "32K tokens"). */
  valueText: (index: number) => string;
  /** Optional stop that gets an extra glow/sparkle (effort's ultracode). */
  boostIndex?: number;
  /** When true, the track is non-interactive and dimmed. */
  disabled?: boolean;
}

interface Particle {
  x: number;
  y: number;
  r: number;
  ph: number;
  sp: number;
  vx: number;
  bright: boolean;
}

export const StardustSlider: React.FC<Props> = ({
  count,
  value,
  onChange,
  readColors,
  colorKey,
  ariaLabel,
  valueText,
  boostIndex = -1,
  disabled = false,
}) => {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const onChangeRef = useRef(onChange);
  const readColorsRef = useRef(readColors);
  const countRef = useRef(count);
  const valueTextRef = useRef(valueText);
  const disabledRef = useRef(disabled);
  useEffect(() => {
    onChangeRef.current = onChange;
    readColorsRef.current = readColors;
    countRef.current = count;
    valueTextRef.current = valueText;
    disabledRef.current = disabled;
  });

  const eng = useRef({
    parts: [] as Particle[],
    disp: Math.max(0, value),
    target: Math.max(0, value),
    col: [150, 150, 150] as RGB,
    palette: [] as RGB[],
    boostIdx: boostIndex,
    track: [30, 30, 36] as RGB,
    hi: [255, 255, 255] as RGB,
    w: 240,
    h: 26,
    dpr: 1,
    raf: 0,
    last: 0,
  }).current;

  const readPalette = () => {
    const root = document.documentElement;
    const cs = getComputedStyle(root);
    const v = (n: string, f: string) => cs.getPropertyValue(n).trim() || f;
    eng.palette = readColorsRef.current();
    eng.track = parseColor(v('--bg-inset', '#15151b'));
    eng.hi = parseColor(v('--text-primary', '#ededeb'));
    if (eng.palette[eng.disp]) eng.col = eng.palette[eng.disp].slice() as RGB;
  };

  useEffect(() => {
    eng.boostIdx = boostIndex;
    readPalette();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [colorKey, boostIndex]);

  useEffect(() => {
    eng.target = Math.max(0, value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const reduce =
      typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    readPalette();

    const rand = (a: number, b: number) => a + Math.random() * (b - a);
    const build = () => {
      eng.w = wrap.clientWidth || 240;
      eng.h = 26;
      eng.dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = eng.w * eng.dpr;
      canvas.height = eng.h * eng.dpr;
      ctx.setTransform(eng.dpr, 0, 0, eng.dpr, 0, 0);
      const n = Math.round(eng.w / 5.5);
      eng.parts = [];
      for (let i = 0; i < n; i++) {
        eng.parts.push({
          x: rand(0, eng.w),
          y: rand(2, eng.h - 2),
          r: rand(0.7, 2.6),
          ph: rand(0, 6.28),
          sp: rand(0.7, 2.4),
          vx: rand(5, 20),
          bright: Math.random() < 0.24,
        });
      }
    };

    const roundRect = (x: number, y: number, w: number, h: number, r: number) => {
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
    };

    const frame = (t: number) => {
      const dt = Math.min((t - eng.last) / 1000 || 0, 0.05);
      eng.last = t;
      const N = countRef.current;
      const W = eng.w;
      const H = eng.h;
      const r = H / 2;
      // Snappy ease so a low→high drag moves fast.
      eng.disp += (eng.target - eng.disp) * (reduce ? 1 : 0.24);
      const tgt = eng.palette[eng.target] || eng.col;
      for (let k = 0; k < 3; k++) eng.col[k] += (tgt[k] - eng.col[k]) * (reduce ? 1 : 0.14);
      const cr = Math.round(eng.col[0]);
      const cg = Math.round(eng.col[1]);
      const cb = Math.round(eng.col[2]);
      const frac = N > 1 ? eng.disp / (N - 1) : 0;
      const orbX = r + frac * (W - 2 * r);
      const cy = H / 2;
      const now = t / 1000;
      const boost = Math.abs(eng.disp - eng.boostIdx) < 0.5 && eng.boostIdx >= 0;
      const tail = Math.min(orbX, W * 0.42 + 22);

      ctx.clearRect(0, 0, W, H);
      ctx.save();
      roundRect(0, 0, W, H, r);
      ctx.clip();
      ctx.fillStyle = `rgb(${Math.round(eng.track[0])},${Math.round(eng.track[1])},${Math.round(eng.track[2])})`;
      ctx.fillRect(0, 0, W, H);

      const sparkleBoost = boost ? 1.7 : 1;
      for (let i = 0; i < eng.parts.length; i++) {
        const p = eng.parts[i];
        if (!reduce) {
          p.x -= p.vx * dt;
          if (p.x < 0) {
            p.x = W;
            p.y = rand(2, H - 2);
          }
        }
        if (p.x > orbX || p.x < orbX - tail) continue;
        const along = (p.x - (orbX - tail)) / tail; // 0 at tail end, 1 near orb
        const twk = reduce ? 0.8 : 0.35 + 0.65 * Math.abs(Math.sin(now * p.sp + p.ph));
        const a = twk * along * (p.bright ? 0.95 * sparkleBoost : 0.7);
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * (p.bright ? sparkleBoost : 1), 0, 6.2832);
        if (p.bright) {
          const b = mix([cr, cg, cb], eng.hi, 0.55);
          ctx.fillStyle = `rgba(${Math.round(b[0])},${Math.round(b[1])},${Math.round(b[2])},${Math.min(a, 1)})`;
        } else {
          ctx.fillStyle = `rgba(${cr},${cg},${cb},${Math.min(a, 1)})`;
        }
        ctx.fill();
      }

      const g = ctx.createRadialGradient(orbX, cy, 0.5, orbX, cy, H * (boost ? 1.15 : 0.9));
      g.addColorStop(0, `rgba(${cr},${cg},${cb},${boost ? 0.7 : 0.55})`);
      g.addColorStop(0.5, `rgba(${cr},${cg},${cb},0.2)`);
      g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);

      ctx.beginPath();
      ctx.arc(orbX, cy, r - 0.5, 0, 6.2832);
      ctx.fillStyle = `rgb(${cr},${cg},${cb})`;
      ctx.fill();
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(255,255,255,0.35)';
      ctx.stroke();

      const pulse = reduce ? 0.6 : 0.4 + 0.35 * Math.abs(Math.sin(now * 2.4));
      const hb = mix([cr, cg, cb], eng.hi, 0.72);
      ctx.beginPath();
      ctx.arc(orbX - r * 0.28, cy - r * 0.28, r * 0.32, 0, 6.2832);
      ctx.fillStyle = `rgba(${Math.round(hb[0])},${Math.round(hb[1])},${Math.round(hb[2])},${pulse})`;
      ctx.fill();
      ctx.restore();

      eng.raf = requestAnimationFrame(frame);
    };

    const idxFromX = (clientX: number) => {
      const N = countRef.current;
      const rc = canvas.getBoundingClientRect();
      const f = (clientX - rc.left) / Math.max(rc.width, 1);
      return Math.max(0, Math.min(N - 1, Math.round(f * (N - 1))));
    };
    const commit = (i: number) => {
      eng.target = i;
      onChangeRef.current(i);
    };
    let down = false;
    const onDown = (e: PointerEvent) => {
      if (disabledRef.current) return;
      down = true;
      try {
        wrap.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      commit(idxFromX(e.clientX));
      e.preventDefault();
    };
    const onMove = (e: PointerEvent) => {
      if (down && !disabledRef.current) commit(idxFromX(e.clientX));
    };
    const onUp = () => {
      down = false;
    };
    const onKey = (e: KeyboardEvent) => {
      if (disabledRef.current) return;
      const N = countRef.current;
      const c = Math.round(eng.target);
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
        commit(Math.min(N - 1, c + 1));
        e.preventDefault();
      }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
        commit(Math.max(0, c - 1));
        e.preventDefault();
      }
      if (e.key === 'Home') {
        commit(0);
        e.preventDefault();
      }
      if (e.key === 'End') {
        commit(N - 1);
        e.preventDefault();
      }
    };

    wrap.addEventListener('pointerdown', onDown);
    wrap.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    wrap.addEventListener('keydown', onKey);
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver === 'function') {
      ro = new ResizeObserver(build);
      ro.observe(wrap);
    } else {
      window.addEventListener('resize', build);
    }
    build();
    eng.raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(eng.raf);
      if (ro) ro.disconnect();
      else window.removeEventListener('resize', build);
      wrap.removeEventListener('pointerdown', onDown);
      wrap.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      wrap.removeEventListener('keydown', onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const idx = Math.max(0, value);
  return (
    <div
      className="effort-stardust-track"
      ref={wrapRef}
      tabIndex={disabled ? -1 : 0}
      role="slider"
      aria-label={ariaLabel}
      aria-valuemin={0}
      aria-valuemax={count - 1}
      aria-valuenow={idx}
      aria-valuetext={valueText(idx)}
      aria-disabled={disabled || undefined}
      style={disabled ? { pointerEvents: 'none', opacity: 0.55 } : undefined}
    >
      <canvas className="effort-stardust" ref={canvasRef} />
    </div>
  );
};
