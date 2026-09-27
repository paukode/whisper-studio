/**
 * GET /api/costs/usage: one ranged report that feeds the whole Costs tab.
 *
 * Days are UTC days (they line up with AWS Cost Explorer), both range ends are
 * inclusive and weeks start on Monday. The runtime check lives in
 * src/types/schemas/costs.schema.ts, which is typed against these interfaces
 * so the two cannot drift apart.
 */

export type UsageGranularity = 'day' | 'week' | 'month';
export type UsageSplit = 'model' | 'session' | 'source';

export interface UsageRange {
  /** YYYY-MM-DD, inclusive. */
  from: string;
  /** YYYY-MM-DD, inclusive. */
  to: string;
  timezone: 'UTC';
}

export interface UsageTotals {
  cost_usd: number;
  /** Uncached input plus cache read plus cache write, the same for every
   *  provider, so GPT and Claude rows add up. */
  prompt_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  calls: number;
  /** Calls whose token counts were estimated (characters / 4 of the payload)
   *  because the provider reported none. */
  estimated_calls: number;
  /** Calls of a billed model with no rate in the pricing table: their spend
   *  shows as $0, which is not what AWS bills. On-device calls are not
   *  counted here (they cost nothing). */
  unpriced_calls: number;
  /** Fraction in [0, 1]: cache reads over prompt tokens. */
  cache_hit_rate: number;
  /** Net of cache-write premiums, at list rates. Negative when the writes
   *  cost more than the reads saved. */
  cache_savings_usd: number;
}

export interface UsageKeyFigures {
  cost_usd: number;
  prompt_tokens: number;
  output_tokens: number;
  calls: number;
}

export interface UsageBucket {
  /** YYYY-MM-DD, the first day the bucket covers inside the range. */
  start: string;
  /** YYYY-MM-DD, the last day the bucket covers inside the range. */
  end: string;
  /** A first or last week or month clipped by the range. */
  partial: boolean;
  cost_usd: number;
  /** Figures per split key (the same keys as `rows[].key`). */
  by_key: Record<string, UsageKeyFigures>;
}

export interface UsageRow {
  key: string;
  label: string;
  cost_usd: number;
  /** Fraction in [0, 1] of the range's cost. */
  share: number;
  prompt_tokens: number;
  /** Percentage in [0, 100] of this row's prompt tokens read from cache. */
  cached_pct: number;
  output_tokens: number;
  calls: number;
  estimated_calls: number;
  /** Pricing note for this row (for example the GPT list-rate note), or ''. */
  note: string;
  /** True for a session row whose session no longer exists. */
  deleted: boolean;
}

export interface UsageResponse {
  range: UsageRange;
  granularity: UsageGranularity;
  split: UsageSplit;
  totals: UsageTotals;
  /** Zero-filled over the whole range. */
  buckets: UsageBucket[];
  rows: UsageRow[];
}
