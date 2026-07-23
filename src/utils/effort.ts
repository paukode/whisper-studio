/* Per-model effort levels — friendly labels, ordering, and clamping.
 *
 * Mirrors server/infrastructure/effort.py. The backend is the source of
 * truth for which levels each model exposes (sent on /api/models as
 * `effort_levels`); this module only owns the display labels and the
 * client-side clamp used when switching models. */

export const EFFORT_ORDER = ['none', 'low', 'medium', 'high', 'extra', 'max', 'ultracode'] as const;
export type EffortLevel = (typeof EFFORT_ORDER)[number];

export const EFFORT_LABELS: Record<string, string> = {
  none: 'None',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  extra: 'Extra',
  max: 'Max',
  ultracode: 'Ultracode',
};

export const DEFAULT_EFFORT = 'high';

/** Friendly display name for a level key (falls back to the raw value). */
export function effortLabel(level: string): string {
  return EFFORT_LABELS[level] ?? level;
}

/** Clamp `level` to the nearest allowed level at or below it — matches the
 *  backend and Claude Code's own fallback rule. Returns `level` unchanged
 *  when the model has no effort support (empty `allowed`). */
export function clampEffort(level: string, allowed: string[]): string {
  if (allowed.length === 0) return level;
  if (allowed.includes(level)) return level;
  let ci = EFFORT_ORDER.indexOf(level as EffortLevel);
  if (ci < 0) ci = EFFORT_ORDER.indexOf(DEFAULT_EFFORT as EffortLevel);
  let best: string | null = null;
  for (const lv of allowed) {
    const i = EFFORT_ORDER.indexOf(lv as EffortLevel);
    if (i <= ci && (best === null || i > EFFORT_ORDER.indexOf(best as EffortLevel))) best = lv;
  }
  return best ?? allowed[0];
}

/** Map a legacy/unknown value (e.g. the retired 'auto') onto a known level. */
export function normalizeEffort(level: string | undefined | null): string {
  return level && (EFFORT_ORDER as readonly string[]).includes(level) ? level : DEFAULT_EFFORT;
}
