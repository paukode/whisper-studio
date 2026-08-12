/**
 * Export helpers. The colour conversion is the load-bearing one: the diagram
 * palette is built from `color-mix()`, which browsers resolve to CSS Color 4
 * `color(srgb …)`. Figma, Illustrator and Inkscape ignore that function, so
 * without this an exported diagram opens as unfilled outlines.
 */
import { describe, it, expect } from 'vitest';
import { toLegacyColor, slugify } from './vizExport';

describe('toLegacyColor', () => {
  it('converts srgb components to 0-255 rgb', () => {
    expect(toLegacyColor('color(srgb 0.240275 0.190314 0.167176)')).toBe('rgb(61, 49, 43)');
    expect(toLegacyColor('color(srgb 0 0 0)')).toBe('rgb(0, 0, 0)');
    expect(toLegacyColor('color(srgb 1 1 1)')).toBe('rgb(255, 255, 255)');
  });

  it('keeps partial alpha and drops a fully opaque one', () => {
    expect(toLegacyColor('color(srgb 1 0 0 / 0.5)')).toBe('rgba(255, 0, 0, 0.5)');
    expect(toLegacyColor('color(srgb 1 0 0 / 1)')).toBe('rgb(255, 0, 0)');
  });

  it('converts every colour in a multi-value declaration', () => {
    expect(toLegacyColor('color(srgb 1 0 0) color(srgb 0 1 0)')).toBe(
      'rgb(255, 0, 0) rgb(0, 255, 0)',
    );
  });

  it('leaves colours other renderers already understand alone', () => {
    for (const value of ['rgb(1, 2, 3)', '#abcdef', 'none', 'rgba(0,0,0,0.08)']) {
      expect(toLegacyColor(value)).toBe(value);
    }
  });
});

describe('slugify', () => {
  it('makes a filename out of a title', () => {
    expect(slugify('Quarterly revenue by region, 2025', 'x')).toBe(
      'quarterly-revenue-by-region-2025',
    );
  });

  it('falls back when a title has nothing usable', () => {
    expect(slugify('!!!', 'visual')).toBe('visual');
    expect(slugify('', 'visual')).toBe('visual');
  });
});
