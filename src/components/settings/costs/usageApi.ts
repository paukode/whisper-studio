import { z } from 'zod';
import { get } from '@/api/client';
import type { UsageGranularity, UsageResponse, UsageSplit } from '@/types/costs';
import { UsageResponseSchema } from '@/types/schemas';
import { usageUrl, type ResolvedRange } from './usageView';

export function usageQueryKey(range: ResolvedRange, granularity: UsageGranularity, split: UsageSplit) {
  return ['costs-usage', range.from, range.to, granularity, split] as const;
}

/**
 * GET /api/costs/usage, validated. A response that does not match the
 * contract, or that answers a different range, granularity or split than the
 * one asked for (a renamed or ignored query parameter falls back to the
 * server default), is an error with the reason in its message, never a
 * quietly empty view or another range's total shown as this one's.
 */
export async function fetchUsage(
  range: ResolvedRange,
  granularity: UsageGranularity,
  split: UsageSplit,
): Promise<UsageResponse> {
  const raw = await get<unknown>(usageUrl(range, granularity, split));
  const parsed = UsageResponseSchema.safeParse(raw);
  if (!parsed.success) {
    throw new Error(
      `the server's usage report does not match what this tab reads. ${z.prettifyError(parsed.error)}`,
    );
  }
  const data = parsed.data;
  if (data.granularity !== granularity || data.split !== split) {
    throw new Error(
      `asked for ${granularity} buckets split by ${split}, but the server answered ` +
        `${data.granularity} buckets split by ${data.split}.`,
    );
  }
  if (data.range.from !== range.from || data.range.to !== range.to) {
    throw new Error(
      `asked for ${range.from} to ${range.to}, but the server answered ` +
        `${data.range.from} to ${data.range.to}.`,
    );
  }
  return data;
}
