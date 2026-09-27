/**
 * The Costs tab's view state (range, granularity, split) and the pure helpers
 * around it: UTC range presets, the request URLs, and number formatting.
 *
 * Days are UTC days so the tab lines up one to one with AWS Cost Explorer.
 * Nothing here reads the local calendar: "Today" at 00:30 in Warsaw is still
 * yesterday's UTC day until 02:00.
 */
import type { UsageGranularity, UsageSplit } from '@/types/costs';
import { STORAGE_KEYS } from '@/utils/storageKeys';

export type RangePreset =
  | 'today'
  | 'last7'
  | 'last30'
  | 'thisMonth'
  | 'lastMonth'
  | 'all'
  | 'custom';

export const RANGE_OPTIONS: ReadonlyArray<{ value: RangePreset; label: string }> = [
  { value: 'today', label: 'Today (UTC)' },
  { value: 'last7', label: 'Last 7 days' },
  { value: 'last30', label: 'Last 30 days' },
  { value: 'thisMonth', label: 'This month' },
  { value: 'lastMonth', label: 'Last month' },
  { value: 'all', label: 'All time' },
  { value: 'custom', label: 'Custom' },
];

export const GRANULARITY_OPTIONS: ReadonlyArray<{ value: UsageGranularity; label: string }> = [
  { value: 'day', label: 'Day' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
];

export const SPLIT_OPTIONS: ReadonlyArray<{ value: UsageSplit; label: string }> = [
  { value: 'model', label: 'Model' },
  { value: 'session', label: 'Session' },
  { value: 'source', label: 'Source' },
];

/** The usage endpoint takes a concrete start and zero-fills every bucket from
 *  it, so "All time" starts on the day of the repository's first commit (the
 *  initial public release). A cost row dated before it would not be counted,
 *  so the tab states that cutoff whenever All time is on screen. */
export const ALL_TIME_FROM = '2026-07-23';

export interface CostsView {
  range: RangePreset;
  /** YYYY-MM-DD, used only when range is 'custom'. */
  customFrom: string;
  customTo: string;
  granularity: UsageGranularity;
  split: UsageSplit;
}

export const DEFAULT_VIEW: CostsView = {
  range: 'last30',
  customFrom: '',
  customTo: '',
  granularity: 'day',
  split: 'model',
};

export interface ResolvedRange {
  from: string;
  to: string;
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A real calendar date in YYYY-MM-DD form (rejects 2026-02-30). */
export function isIsoDate(value: string): boolean {
  const m = ISO_DATE.exec(value);
  if (!m) return false;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.toISOString().slice(0, 10) === value;
}

function toIso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function parseIso(value: string): Date {
  const m = ISO_DATE.exec(value);
  if (!m) throw new Error(`not a YYYY-MM-DD date: ${value}`);
  return new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
}

/** Today's UTC calendar day. */
export function utcToday(now: Date): string {
  return toIso(now);
}

export function addDays(iso: string, days: number): string {
  const d = parseIso(iso);
  d.setUTCDate(d.getUTCDate() + days);
  return toIso(d);
}

/** The dates a preset covers on `now`'s UTC day, both ends inclusive. */
export function presetRange(preset: Exclude<RangePreset, 'custom'>, now: Date): ResolvedRange {
  const today = utcToday(now);
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth();
  switch (preset) {
    case 'today':
      return { from: today, to: today };
    case 'last7':
      return { from: addDays(today, -6), to: today };
    case 'last30':
      return { from: addDays(today, -29), to: today };
    case 'thisMonth':
      return { from: toIso(new Date(Date.UTC(y, m, 1))), to: today };
    case 'lastMonth':
      return {
        from: toIso(new Date(Date.UTC(y, m - 1, 1))),
        // Day 0 of this month is the last day of the previous one.
        to: toIso(new Date(Date.UTC(y, m, 0))),
      };
    case 'all':
      return { from: ALL_TIME_FROM, to: today };
  }
}

/** The range to request, or the reason there is none (a custom range the
 *  user has not finished, or one that runs backwards). */
export function resolveRange(
  view: CostsView,
  now: Date,
): { ok: true; range: ResolvedRange } | { ok: false; reason: string } {
  if (view.range !== 'custom') return { ok: true, range: presetRange(view.range, now) };
  if (!isIsoDate(view.customFrom) || !isIsoDate(view.customTo)) {
    return { ok: false, reason: 'Pick both a From and a To date.' };
  }
  if (view.customFrom > view.customTo) {
    return { ok: false, reason: 'From must be on or before To.' };
  }
  return { ok: true, range: { from: view.customFrom, to: view.customTo } };
}

export function usageUrl(
  range: ResolvedRange,
  granularity: UsageGranularity,
  split: UsageSplit,
): string {
  const q = new URLSearchParams({ from: range.from, to: range.to, granularity, split });
  return `/api/costs/usage?${q.toString()}`;
}

export function exportUrl(range: ResolvedRange, format: 'csv' | 'json'): string {
  const q = new URLSearchParams({ from: range.from, to: range.to, format });
  return `/api/costs/export?${q.toString()}`;
}

/* ── Remembered view (a per-viewer convenience; every access is guarded) ── */

const PRESETS = new Set<string>(RANGE_OPTIONS.map((o) => o.value));
const GRANULARITIES = new Set<string>(GRANULARITY_OPTIONS.map((o) => o.value));
const SPLITS = new Set<string>(SPLIT_OPTIONS.map((o) => o.value));

export function loadCostsView(): CostsView {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.COSTS_VIEW);
    const saved: unknown = raw ? JSON.parse(raw) : null;
    if (!saved || typeof saved !== 'object') return DEFAULT_VIEW;
    const s = saved as Record<string, unknown>;
    return {
      range: typeof s.range === 'string' && PRESETS.has(s.range) ? (s.range as RangePreset) : DEFAULT_VIEW.range,
      customFrom: typeof s.customFrom === 'string' && isIsoDate(s.customFrom) ? s.customFrom : '',
      customTo: typeof s.customTo === 'string' && isIsoDate(s.customTo) ? s.customTo : '',
      granularity:
        typeof s.granularity === 'string' && GRANULARITIES.has(s.granularity)
          ? (s.granularity as UsageGranularity)
          : DEFAULT_VIEW.granularity,
      split: typeof s.split === 'string' && SPLITS.has(s.split) ? (s.split as UsageSplit) : DEFAULT_VIEW.split,
    };
  } catch {
    // Storage unavailable or holding junk: start from the defaults.
    return DEFAULT_VIEW;
  }
}

export function saveCostsView(view: CostsView): void {
  try {
    localStorage.setItem(STORAGE_KEYS.COSTS_VIEW, JSON.stringify(view));
  } catch {
    // Storage unavailable: the choice lasts until the panel closes.
  }
}

/* ── Formatting ── */

/** Two decimals from $1 up, four below, so small spend is still readable
 *  (exactly zero stays "$0.00"). */
export function fmtMoney(value: number): string {
  const abs = Math.abs(value);
  const digits = abs >= 1 || abs === 0 ? 2 : 4;
  const text = abs.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${value < 0 ? '-' : ''}$${text}`;
}

export function fmtCount(value: number): string {
  return Math.round(value).toLocaleString('en-US');
}

/** A [0, 1] fraction as a percentage with one decimal. */
export function fmtFraction(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Sep 1" or, with the year, "Sep 1, 2026". Read straight from the string,
 *  so no local time zone can shift the day. */
export function fmtDay(iso: string, withYear = false): string {
  const d = parseIso(iso);
  const base = `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  return withYear ? `${base}, ${d.getUTCFullYear()}` : base;
}

export function fmtRange(from: string, to: string): string {
  if (from === to) return fmtDay(from, true);
  return `${fmtDay(from, true)} to ${fmtDay(to, true)}`;
}

/** A chart bucket's name: the day, the week's dates, or the month. A partial
 *  bucket always shows the dates it was clipped to. */
export function bucketLabel(
  bucket: { start: string; end: string; partial: boolean },
  granularity: UsageGranularity,
): string {
  if (granularity === 'day') return fmtDay(bucket.start);
  if (granularity === 'month' && !bucket.partial) {
    const d = parseIso(bucket.start);
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  }
  const dates = bucket.start === bucket.end ? fmtDay(bucket.start) : `${fmtDay(bucket.start)} to ${fmtDay(bucket.end)}`;
  return bucket.partial ? `${dates} (partial)` : dates;
}
