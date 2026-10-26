import React, { useMemo, useState } from 'react';
import { useTheme } from '@/providers/ThemeProvider';
import { paletteTokens, type Mode, type Palette } from '@/utils/paletteTokens';
import type { ThemeKey } from '@/types/theme';

type ToneFilter = 'all' | Palette['tone'];

const TONE_FILTERS: ReadonlyArray<{ id: ToneFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'warm', label: 'Warm' },
  { id: 'cool', label: 'Cool' },
  { id: 'neutral', label: 'Neutral' },
];

/** A small drawing of the app in one mode of a palette: canvas, a card with
 *  a heading and text lines, an accent button and the status colors. */
const MiniPreview: React.FC<{ palette: Palette; mode: Mode }> = ({ palette, mode }) => {
  const t = useMemo(() => paletteTokens(palette[mode], mode), [palette, mode]);
  return (
    <div className="palette-preview" style={{ background: t['--bg-primary'] }} aria-hidden="true">
      <div className="palette-preview-card" style={{ background: t['--bg-surface'], borderColor: t['--border'] }}>
        <span className="palette-preview-line is-heading" style={{ background: t['--text-heading'] }} />
        <span className="palette-preview-line" style={{ background: t['--text-primary'] }} />
        <span className="palette-preview-line is-short" style={{ background: t['--text-muted'] }} />
        <span className="palette-preview-row">
          <span className="palette-preview-button" style={{ background: t['--accent'] }} />
          <span className="palette-preview-dot" style={{ background: t['--accent-live'] }} />
          <span className="palette-preview-dot" style={{ background: t['--warning'] }} />
          <span className="palette-preview-dot" style={{ background: t['--accent-record'] }} />
          <span className="palette-preview-dot" style={{ background: t['--agent'] }} />
        </span>
      </div>
    </div>
  );
};

/** Settings > Appearance: the light/dark mode and the color palette. */
export const AppearancePanel: React.FC = () => {
  const { themeKey, setTheme, themes, paletteId, setPalette, palettes, resolvedTheme } = useTheme();
  const [filter, setFilter] = useState<ToneFilter>('all');
  const shown = filter === 'all' ? palettes : palettes.filter((p) => p.tone === filter);
  const active = palettes.find((p) => p.id === paletteId);

  return (
    <div className="settings-panel appearance-panel">
      <section className="appearance-section">
        <h3 className="appearance-heading">Mode</h3>
        <div className="appearance-modes" role="radiogroup" aria-label="Mode">
          {themes.map((t) => (
            <button
              key={t.key}
              type="button"
              role="radio"
              aria-checked={themeKey === t.key}
              className={`appearance-mode${themeKey === t.key ? ' active' : ''}`}
              onClick={() => setTheme(t.key as ThemeKey)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="settings-hint">System follows the macOS appearance, light by day and dark by night.</span>
      </section>

      <section className="appearance-section">
        <div className="appearance-heading-row">
          <h3 className="appearance-heading">Color palette</h3>
          {active && <span className="appearance-current">{active.name}</span>}
        </div>
        <span className="settings-hint">
          Every palette has a light and a dark mode. All of them keep text readable, avoid pure white
          and pure black, and stay soft on the eyes at night.
        </span>
        <div className="appearance-filters" role="group" aria-label="Filter palettes">
          {TONE_FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              aria-pressed={filter === f.id}
              className={`appearance-filter${filter === f.id ? ' active' : ''}`}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="palette-grid" role="radiogroup" aria-label="Color palette">
          {shown.map((p) => {
            const selected = p.id === paletteId;
            return (
              <button
                key={p.id}
                type="button"
                role="radio"
                aria-checked={selected}
                className={`palette-card${selected ? ' active' : ''}`}
                onClick={() => setPalette(p.id)}
                title={p.note}
              >
                <span className={`palette-previews${resolvedTheme === 'dark' ? ' dark-first' : ''}`}>
                  <MiniPreview palette={p} mode="light" />
                  <MiniPreview palette={p} mode="dark" />
                </span>
                <span className="palette-card-meta">
                  <span className="palette-card-name">{p.name}</span>
                  <span className="palette-card-tone">{p.tone}</span>
                  {selected && (
                    <svg className="palette-card-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M20 6L9 17l-5-5" />
                    </svg>
                  )}
                </span>
                <span className="palette-card-note">{p.note}</span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
};
