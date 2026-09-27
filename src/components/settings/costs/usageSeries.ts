/**
 * Naming and stacking for the Costs tab: what a row is called, which split
 * keys get their own colour in the chart, and how one bucket stacks.
 */
import type {
  UsageBucket,
  UsageKeyFigures,
  UsageRow,
  UsageSplit,
} from '@/types/costs';

/** Split keys drawn in their own colour; the rest stack as "Other". */
export const TOP_SERIES = 5;
export const OTHER_KEY = '__other__';

export interface ChartSeries {
  key: string;
  label: string;
  /** Colour slot 0..TOP_SERIES-1, or 'other'. */
  slot: number | 'other';
  /** Some of this series' calls in the range have estimated token counts.
   *  Buckets carry no per-bucket estimate count, so this is range-wide. */
  estimated: boolean;
}

export interface Segment extends UsageKeyFigures {
  key: string;
}

/**
 * The name a row shows. The server's label wins whenever it says more than
 * the raw key. Model rows the server cannot name (local models it has no
 * catalog entry for) fall back to the model picker's name. A session row with
 * no saved session behind it and no label of its own reads "Deleted session":
 * its spend stays in every total, so it must still appear. `deleted` only
 * says no sessions row exists, which is also true of ids that never were
 * saved sessions (a memory dream, a named headless run), so a label the
 * server gives those is kept rather than overwritten.
 */
export function rowName(
  row: UsageRow,
  split: UsageSplit,
  pickerName: (key: string) => string | undefined,
): string {
  if (row.label && row.label !== row.key) return row.label;
  if (split === 'session' && row.deleted) return 'Deleted session';
  if (split === 'model') return pickerName(row.key) ?? row.key;
  return row.label || row.key;
}

/** The top keys by cost for the range, plus "Other" whenever any spend (in
 *  the rows or in a bucket) belongs to a key outside them. Two keys that
 *  would share a name (two deleted sessions) carry their raw key as well, so
 *  the legend and the tooltip can tell them apart. */
export function chartSeries(
  rows: UsageRow[],
  buckets: UsageBucket[],
  nameOf: (row: UsageRow) => string,
): ChartSeries[] {
  const ranked = [...rows].sort(
    (a, b) =>
      b.cost_usd - a.cost_usd || b.prompt_tokens - a.prompt_tokens || a.key.localeCompare(b.key),
  );
  const top = ranked.slice(0, TOP_SERIES);
  const topKeys = new Set(top.map((r) => r.key));
  const names = top.map(nameOf);
  const shared = new Set(names.filter((n, i) => names.indexOf(n) !== i));
  const series: ChartSeries[] = top.map((r, i) => ({
    key: r.key,
    label: shared.has(names[i]) ? `${names[i]} (${r.key})` : names[i],
    slot: i,
    estimated: r.estimated_calls > 0,
  }));
  const rest = ranked.slice(TOP_SERIES);
  const hasOther =
    rest.length > 0 || buckets.some((b) => Object.keys(b.by_key).some((k) => !topKeys.has(k)));
  if (hasOther) {
    series.push({
      key: OTHER_KEY,
      label: 'Other',
      slot: 'other',
      estimated: rest.some((r) => r.estimated_calls > 0),
    });
  }
  return series;
}

const ZERO: UsageKeyFigures = { cost_usd: 0, prompt_tokens: 0, output_tokens: 0, calls: 0 };

/** One bucket's figures per series, in series order (bottom of the bar first). */
export function bucketSegments(bucket: UsageBucket, series: ChartSeries[]): Segment[] {
  const named = new Set(series.filter((s) => s.slot !== 'other').map((s) => s.key));
  return series.map((s) => {
    if (s.slot !== 'other') return { key: s.key, ...(bucket.by_key[s.key] ?? ZERO) };
    const other = { key: s.key, ...ZERO };
    for (const [key, fig] of Object.entries(bucket.by_key)) {
      if (named.has(key)) continue;
      other.cost_usd += fig.cost_usd;
      other.prompt_tokens += fig.prompt_tokens;
      other.output_tokens += fig.output_tokens;
      other.calls += fig.calls;
    }
    return other;
  });
}

export function seriesClass(slot: ChartSeries['slot']): string {
  return slot === 'other' ? 'usage-series-other' : `usage-series-${slot}`;
}

/** The element id of pricing note number `n` (1-based), for a row to point at. */
export function noteId(n: number): string {
  return `costsNote${n}`;
}

/** The colour slot a table row shares with the chart: its own series, or
 *  "Other" for a key outside the top ones. */
export function slotOf(key: string, series: ChartSeries[]): ChartSeries['slot'] {
  return series.find((s) => s.key === key)?.slot ?? 'other';
}
