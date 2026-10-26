import React, { useCallback } from 'react';
import { useTheme } from '@/providers/ThemeProvider';
import { effortLabel } from '@/utils/effort';
import { StardustSlider, parseColor, mix, type RGB } from '@/components/chat/StardustSlider';

/**
 * The effort control's track: the shared {@link StardustSlider} driven by the
 * effort tiers. Each tier maps to a hue read from the live theme: the
 * --tone-blue/orange/violet tokens for low/high/extra, --text-muted for medium/none, --accent
 * for max, and a lightened accent for ultracode (which also gets the extra orb
 * glow via `boostIndex`). Interaction writes the level to the same store the
 * chip / `/effort` / palette use.
 */
const TONES: Record<string, [string, string]> = {
  low: ['--tone-blue', '#7cb7f0'],
  high: ['--tone-orange', '#f5a25d'],
  extra: ['--tone-violet', '#b49cff'],
};

interface Props {
  levels: string[];
  value: string;
  onChange: (level: string) => void;
  /** Display name of a level; the effort labels by default. */
  labelFor?: (level: string) => string;
  ariaLabel?: string;
}

export const StardustEffortSlider: React.FC<Props> = ({
  levels,
  value,
  onChange,
  labelFor = effortLabel,
  ariaLabel = 'Effort level',
}) => {
  const { resolvedTheme } = useTheme();

  const readColors = useCallback((): RGB[] => {
    const cs = getComputedStyle(document.documentElement);
    const v = (n: string, f: string) => cs.getPropertyValue(n).trim() || f;
    const accent = parseColor(v('--accent', '#9a82ff'));
    return levels.map((lv) => {
      if (TONES[lv]) return parseColor(v(...TONES[lv]));
      if (lv === 'medium' || lv === 'none') return parseColor(v('--text-muted', '#8c8bab'));
      if (lv === 'max') return accent;
      if (lv === 'ultracode') return mix(accent, [255, 255, 255], 0.32);
      return accent;
    });
  }, [levels]);

  return (
    <StardustSlider
      count={levels.length}
      value={Math.max(0, levels.indexOf(value))}
      onChange={(i) => onChange(levels[i])}
      readColors={readColors}
      colorKey={`${resolvedTheme}:${levels.join(',')}`}
      ariaLabel={ariaLabel}
      valueText={(i) => labelFor(levels[i] ?? value)}
      boostIndex={levels.indexOf('ultracode')}
    />
  );
};
