import { z } from 'zod';
import type { UsageResponse } from '@/types/costs';

/* GET /api/costs/usage. Every field is required: the Costs tab parses the
   response with this schema and shows the failure, so a server that renames or
   drops a field (the old /daily endpoint answered `days` while the tab read
   `daily`, and the chart stayed empty for months) fails loudly instead of
   rendering blanks. The bounds catch a unit mix-up between fractions and
   percentages; the small slack absorbs float rounding on a 100 percent row. */

// Token counts are not forced to integers: an estimated count is characters
// divided by 4 of the payload, which the server may keep unrounded.
const tokens = z.number().nonnegative();
const count = z.number().int().nonnegative();
const money = z.number().nonnegative();
const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'expected YYYY-MM-DD');
const fraction = z.number().min(0).max(1 + 1e-6);
const percent = z.number().min(0).max(100 + 1e-4);

const UsageKeyFiguresSchema = z.object({
  cost_usd: money,
  prompt_tokens: tokens,
  output_tokens: tokens,
  calls: count,
});

export const UsageResponseSchema: z.ZodType<UsageResponse> = z.object({
  range: z.object({
    from: isoDate,
    to: isoDate,
    timezone: z.literal('UTC'),
  }),
  granularity: z.enum(['day', 'week', 'month']),
  split: z.enum(['model', 'session', 'source']),
  totals: z.object({
    cost_usd: money,
    prompt_tokens: tokens,
    cache_read_tokens: tokens,
    cache_write_tokens: tokens,
    output_tokens: tokens,
    calls: count,
    estimated_calls: count,
    unpriced_calls: count,
    cache_hit_rate: fraction,
    // Net of cache-write premiums, so it can be negative.
    cache_savings_usd: z.number(),
  }),
  buckets: z.array(
    z.object({
      start: isoDate,
      end: isoDate,
      partial: z.boolean(),
      cost_usd: money,
      by_key: z.record(z.string(), UsageKeyFiguresSchema),
    }),
  ),
  rows: z.array(
    z.object({
      key: z.string(),
      label: z.string(),
      cost_usd: money,
      share: fraction,
      prompt_tokens: tokens,
      cached_pct: percent,
      output_tokens: tokens,
      calls: count,
      estimated_calls: count,
      note: z.string(),
      deleted: z.boolean(),
    }),
  ),
});
