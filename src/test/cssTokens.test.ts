/**
 * CSS custom-property guard (UI modernization Phase 0).
 *
 * Every `var(--x)` used in the static stylesheets must be defined somewhere
 * in them — an undefined variable silently falls back to the inherited or
 * initial value, which is how the app shipped with unstyled fonts, status
 * colors, and surfaces for months without anyone noticing.
 *
 * If this test fails after you add CSS: either define the variable in
 * static/modules/themes.css (preferred — next to the other tokens), or add
 * it to RUNTIME_DEFINED if JavaScript sets it via style.setProperty at
 * runtime.
 *
 * Stylesheets are pulled in via Vite raw imports (import.meta.glob) so the
 * test needs no Node fs access and runs identically in jsdom.
 */
import { describe, it, expect } from 'vitest';

/** Variables set from JS at runtime rather than in a stylesheet. */
const RUNTIME_DEFINED = new Set<string>([]);

// static/dist is deliberately excluded — it's the built bundle.
const sheets = import.meta.glob<string>(
  ['../../static/style.css', '../../static/modules/*.css'],
  { query: '?raw', import: 'default', eager: true },
);

describe('CSS custom properties', () => {
  it('defines every var(--x) used in the static stylesheets', () => {
    expect(Object.keys(sheets).length).toBeGreaterThan(1);

    const defined = new Set<string>();
    const used = new Map<string, string[]>(); // var -> files using it

    for (const [file, css] of Object.entries(sheets)) {
      for (const m of css.matchAll(/(?:^|[{;\s])(--[a-zA-Z0-9_-]+)\s*:/g)) {
        defined.add(m[1]);
      }
      for (const m of css.matchAll(/var\(\s*(--[a-zA-Z0-9_-]+)/g)) {
        const files = used.get(m[1]) ?? [];
        if (!files.includes(file)) files.push(file);
        used.set(m[1], files);
      }
    }

    const missing = [...used.entries()]
      .filter(([name]) => !defined.has(name) && !RUNTIME_DEFINED.has(name))
      .map(([name, files]) => `${name} (used in ${files.join(', ')})`)
      .sort();

    expect(missing).toEqual([]);
  });
});
