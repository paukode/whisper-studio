import React, { useCallback } from 'react';
import { useTheme } from '@/providers/ThemeProvider';
import { effortLabel } from '@/utils/effort';
import { StardustSlider, parseColor, mix, type RGB } from '@/components/chat/StardustSlider';

/**
 * The effort control's track: the shared {@link StardustSlider} driven by the
 * effort tiers. Each tier maps to a hue read from the live theme — the fixed
 * blue/amber/violet for low/high/extra, --text-muted for medium/none, --accent
 * for max, and a lightened accent for ultracode (which also gets the extra orb
 * glow via `boostIndex`). Interaction writes the level to the same store the
 * chip / `/effort` / palette use.
 */
const FIXED: Record<string, string> = { low: '#6ea8fe', high: '#ffa657', extra: '#c4a7ff' };

interface Props {
  levels: string[];
  value: string;
  onChange: (level: string) => void;
}

export const StardustEffortSlider: React.FC<Props> = ({ levels, value, onChange }) => {
  const { resolvedTheme } = useTheme();

  const readColors = useCallback((): RGB[] => {
    const cs = getComputedStyle(document.documentElement);
    const v = (n: string, f: string) => cs.getPropertyValue(n).trim() || f;
    const accent = parseColor(v('--accent', '#e2a336'));
    return levels.map((lv) => {
      if (FIXED[lv]) return parseColor(FIXED[lv]);
      if (lv === 'medium' || lv === 'none') return parseColor(v('--text-muted', '#9a9992'));
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
      ariaLabel="Effort level"
      valueText={(i) => effortLabel(levels[i] ?? value)}
      boostIndex={levels.indexOf('ultracode')}
    />
  );
};
