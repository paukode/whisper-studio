import { describe, it, expect } from 'vitest';
import { DEFAULT_PALETTE_ID, PALETTES, findPalette } from './palettes';
import { SMALL_TEXT_FLOOR, TEXT_FLOOR, contrast, luminance, paletteCss, paletteTokens } from './paletteTokens';

const themesCss = Object.values(
  import.meta.glob<string>('../../static/modules/themes.css', { query: '?raw', import: 'default', eager: true }),
)[0];

/** The token names one mode block of themes.css sets. */
function cssModeTokens(mode: 'light' | 'dark'): string[] {
  const block = themesCss.match(new RegExp(`:root\\[data-theme="${mode}"\\]\\s*\\{([^}]*)\\}`))?.[1] ?? '';
  return [...block.matchAll(/(--[a-z0-9-]+|color-scheme)\s*:/g)].map((m) => m[1]).sort();
}

const cases = PALETTES.flatMap((p) => (['light', 'dark'] as const).map((mode) => ({ p, mode })));

describe('palettes', () => {
  it('have unique ids and include the default', () => {
    const ids = PALETTES.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toContain(DEFAULT_PALETTE_ID);
    expect(findPalette('no-such-palette').id).toBe(DEFAULT_PALETTE_ID);
  });

  it.each(cases)('$p.id $mode sets every token themes.css sets', ({ p, mode }) => {
    expect(Object.keys(paletteTokens(p[mode], mode)).sort()).toEqual(cssModeTokens(mode));
  });

  it.each(cases)('$p.id $mode keeps text readable', ({ p, mode }) => {
    const t = paletteTokens(p[mode], mode);
    for (const surface of ['--bg-primary', '--bg-surface', '--bg-elevated', '--bg-hover']) {
      expect(contrast(t['--text-primary'], t[surface]), `text on ${surface}`).toBeGreaterThanOrEqual(TEXT_FLOOR);
      for (const fg of ['--text-muted', '--text-secondary', '--accent', '--link', '--accent-record',
        '--accent-live', '--warning', '--info', '--agent', '--risk-warn']) {
        expect(contrast(t[fg], t[surface]), `${fg} on ${surface}`).toBeGreaterThanOrEqual(SMALL_TEXT_FLOOR);
      }
    }
    for (const hl of Object.keys(t).filter((k) => k.startsWith('--hl-'))) {
      expect(contrast(t[hl], t['--bg-code']), `${hl} on code`).toBeGreaterThanOrEqual(SMALL_TEXT_FLOOR);
    }
    expect(contrast(t['--on-accent'], t['--accent']), 'button label').toBeGreaterThanOrEqual(SMALL_TEXT_FLOOR);
  });

  it.each(cases)('$p.id $mode is easy on the eyes at night', ({ p, mode }) => {
    const t = paletteTokens(p[mode], mode);
    const canvas = luminance(t['--bg-primary']);
    if (mode === 'dark') {
      // Not pure black (smearing on scroll), not a gray that lights the room.
      expect(canvas).toBeGreaterThan(0.005);
      expect(canvas).toBeLessThan(0.05);
      // No pure white letters, and no glare from too much contrast.
      expect(luminance(t['--text-heading'])).toBeLessThan(0.92);
      expect(contrast(t['--text-primary'], t['--bg-primary'])).toBeLessThan(15);
    } else {
      // No pure white sheet: the canvas and the brightest card stay dimmed.
      expect(canvas).toBeLessThan(0.9);
      expect(luminance(t['--bg-elevated'])).toBeLessThan(0.97);
      expect(t['--text-heading']).not.toBe('#000000');
    }
  });

  it('builds a stylesheet that outranks themes.css in both modes', () => {
    const css = paletteCss(findPalette('nord'));
    expect(css).toContain('html:root[data-theme="dark"]');
    expect(css).toContain('html:root[data-theme="light"]');
  });
});
