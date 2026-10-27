/**
 * Turns a palette's few seed colors into the full set of theme tokens.
 *
 * static/modules/themes.css defines every token for the default palette.
 * Another palette is a stylesheet of the same tokens, built here and put in
 * <style id="ws-palette">. Its selectors outrank the themes.css blocks, so
 * the light/dark switch keeps working with no JavaScript on a mode change.
 *
 * Every text-like color is pushed until it meets a contrast floor against
 * the surface it sits on, so a palette can never ship unreadable text.
 */

export type Mode = 'light' | 'dark';

/** The colors a palette picks for one mode. Everything else is derived. */
export interface ModeSeed {
  /** The page canvas. */
  bg: string;
  /** Body text. */
  text: string;
  /** Headings and the strongest text. */
  heading: string;
  /** Hints, timestamps, placeholders. */
  muted: string;
  /** Buttons, links, active items. */
  accent: string;
  /** The second accent (gradients, functions in code). */
  accent2: string;
  red: string;
  green: string;
  amber: string;
  blue: string;
  violet: string;
  pink: string;
  orange: string;
  /** Light mode only: the color cards lift toward. Defaults to white. */
  paper?: string;
}

export interface Palette {
  id: string;
  name: string;
  /** One short line for the Settings card. */
  note: string;
  tone: 'warm' | 'cool' | 'neutral';
  light: ModeSeed;
  dark: ModeSeed;
}

/* ---- Color math ---- */

type RGB = [number, number, number];

export function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as RGB;
}

function rgbToHex([r, g, b]: RGB): string {
  return '#' + [r, g, b].map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, '0')).join('');
}

/** Mix `a` toward `b` by `t` (0 keeps a, 1 gives b). */
export function mix(a: string, b: string, t: number): string {
  const x = hexToRgb(a);
  const y = hexToRgb(b);
  return rgbToHex([0, 1, 2].map((i) => x[i] + (y[i] - x[i]) * t) as RGB);
}

export function luminance(hex: string): number {
  const lin = hexToRgb(hex).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

export function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Move `fg` toward `toward` in small steps until it reaches `ratio` on `bg`. */
function ensure(fg: string, bg: string, ratio: number, toward: string): string {
  let out = fg;
  for (let t = 0.04; contrast(out, bg) < ratio && t <= 1; t += 0.04) {
    out = mix(fg, toward, t);
  }
  return out;
}

function rgba(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/* ---- Contrast floors ---- */

/** Body text on any surface. */
export const TEXT_FLOOR = 7;
/** Muted text, links, status and syntax colors. */
export const SMALL_TEXT_FLOOR = 4.5;

/* ---- Tokens ---- */

/** Every token one mode block of themes.css sets, built from a seed. */
export function paletteTokens(seed: ModeSeed, mode: Mode): Record<string, string> {
  const dark = mode === 'dark';
  const bg = seed.bg;
  const heading = seed.heading;
  const paper = seed.paper ?? '#ffffff';

  // Dark surfaces lift toward the text color; light cards lift toward paper
  // and hover and borders sink toward the heading ink.
  const lift = (t: number) => mix(bg, seed.text, t);
  const sink = (t: number) => mix(bg, heading, t);
  const secondary = dark ? lift(0.035) : mix(bg, paper, 0.35);
  const surface = dark ? lift(0.055) : mix(bg, paper, 0.6);
  const elevated = dark ? lift(0.085) : mix(bg, paper, 0.75);
  const hover = dark ? lift(0.12) : sink(0.05);
  const inset = dark ? mix(bg, '#000000', 0.22) : sink(0.03);
  const border = dark ? lift(0.13) : sink(0.08);
  const borderStrong = dark ? lift(0.21) : sink(0.15);

  // Text-like colors are checked against the hover row, the hardest surface
  // they appear on in both modes (brightest in dark, darkest in light).
  const away = dark ? '#ffffff' : '#000000';
  const read = (c: string, floor = SMALL_TEXT_FLOOR) => ensure(c, hover, floor, away);

  const text = read(seed.text, TEXT_FLOOR);
  const muted = read(seed.muted);
  const textSecondary = read(mix(text, muted, 0.5));
  const accent = dark ? read(seed.accent) : ensure(read(seed.accent), '#ffffff', SMALL_TEXT_FLOOR, away);
  const accentHover = dark ? mix(accent, heading, 0.25) : mix(accent, heading, 0.2);
  const onAccent = dark ? ensure(bg, accent, SMALL_TEXT_FLOOR, '#000000') : '#ffffff';
  const red = read(seed.red);
  const green = read(seed.green);
  const amber = read(seed.amber);
  const blue = read(seed.blue);
  const violet = read(seed.violet);
  const pink = read(seed.pink);
  const orange = read(seed.orange);
  const accent2 = read(seed.accent2);

  const code = (c: string) => ensure(c, secondary, SMALL_TEXT_FLOOR, away);
  const shadowInk = dark ? '#000000' : heading;

  return {
    'color-scheme': mode,
    '--bg-primary': bg,
    '--bg-surface': surface,
    '--bg-elevated': elevated,
    '--bg-secondary': secondary,
    '--bg-hover': hover,
    '--bg-inset': inset,
    '--bg-code': secondary,
    '--border': border,
    '--border-strong': borderStrong,
    '--text-primary': text,
    '--text-heading': heading,
    '--text-secondary': textSecondary,
    '--text-muted': muted,
    '--accent': accent,
    '--accent-2': accent2,
    '--accent-dim': rgba(accent, dark ? 0.14 : 0.1),
    '--accent-hover': accentHover,
    '--on-accent': onAccent,
    '--link': dark ? accentHover : accent,
    '--link-hover': mix(accent, heading, dark ? 0.45 : 0.35),
    '--accent-record': red,
    '--accent-record-dim': rgba(red, dark ? 0.12 : 0.08),
    '--accent-live': green,
    '--accent-send': accent,
    '--accent-send-hover': accentHover,
    // The user's message is a tinted bubble; the answer sits on the page.
    '--chat-user-bg': rgba(accent, dark ? 0.14 : 0.1),
    '--chat-user-border': rgba(accent, dark ? 0.3 : 0.22),
    '--chat-assistant-bg': 'transparent',
    '--shadow-sm': `0 2px 10px ${rgba(shadowInk, dark ? 0.3 : 0.08)}`,
    '--shadow-md': `0 8px 30px ${rgba(shadowInk, dark ? 0.35 : 0.1)}`,
    '--shadow-lg': `0 14px 48px ${rgba(shadowInk, dark ? 0.45 : 0.16)}`,
    '--noise': 'none',
    '--on-success': dark ? ensure(bg, green, SMALL_TEXT_FLOOR, '#000000') : '#ffffff',
    '--warning': amber,
    '--warning-dim': rgba(amber, dark ? 0.1 : 0.12),
    '--warning-border': rgba(amber, 0.3),
    '--danger-dim': rgba(red, dark ? 0.1 : 0.07),
    '--danger-border': rgba(red, dark ? 0.35 : 0.25),
    '--info': blue,
    '--info-dim': rgba(blue, dark ? 0.1 : 0.08),
    '--info-border': rgba(blue, dark ? 0.35 : 0.28),
    '--success-border': rgba(green, dark ? 0.3 : 0.25),
    '--agent': blue,
    '--agent-dim': rgba(blue, dark ? 0.12 : 0.1),
    '--risk-warn': dark ? amber : orange,
    '--risk-warn-dim': rgba(orange, dark ? 0.12 : 0.14),
    '--mark-bg': dark ? mix(bg, amber, 0.3) : mix(surface, seed.amber, 0.25),
    '--tone-blue': blue,
    '--tone-orange': orange,
    '--tone-violet': violet,
    '--tone-gold': amber,
    '--kbd-bg': dark ? elevated : inset,
    '--chip': dark ? hover : mix(bg, hover, 0.5),
    '--scrollbar': borderStrong,
    '--hl-keyword': code(seed.violet),
    '--hl-string': code(seed.green),
    '--hl-number': code(seed.orange),
    '--hl-literal': code(seed.blue),
    '--hl-comment': code(seed.muted),
    '--hl-function': code(seed.accent2),
    '--hl-class': code(seed.pink),
    '--hl-builtin': code(seed.blue),
    '--hl-attr': code(seed.amber),
    '--hl-tag': code(seed.red),
    '--hl-variable': text,
    '--hl-meta': muted,
    '--hl-addition': green,
    '--hl-deletion': red,
    '--hl-punct': muted,
    '--speaker-1': accent,
    '--speaker-2': blue,
    '--speaker-3': green,
    '--speaker-4': orange,
    '--speaker-5': pink,
    '--speaker-6': amber,
  };
}

/** The stylesheet for one palette, both modes. `html:root` outranks the
 *  `:root[data-theme]` blocks in themes.css whatever the load order, which
 *  matters because index.html adds this before the stylesheets load. */
export function paletteCss(palette: Palette): string {
  return (['dark', 'light'] as const)
    .map((mode) => {
      const body = Object.entries(paletteTokens(palette[mode], mode))
        .map(([k, v]) => `  ${k}: ${v};`)
        .join('\n');
      return `html:root[data-theme="${mode}"] {\n${body}\n}`;
    })
    .join('\n');
}
