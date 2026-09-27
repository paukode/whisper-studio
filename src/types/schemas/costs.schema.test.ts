import { describe, expect, it } from 'vitest';
import { UsageResponseSchema } from './costs.schema';
import { usageFixture } from '@/test/fixtures/costsUsage';

/** The response example from the cross-branch contract, verbatim, plus the
 *  totals' unpriced_calls added since (billed calls with no rate). */
const CONTRACT_EXAMPLE = {
  range: { from: '2026-09-01', to: '2026-09-23', timezone: 'UTC' },
  granularity: 'day',
  split: 'model',
  totals: {
    cost_usd: 0.0, prompt_tokens: 0, cache_read_tokens: 0,
    cache_write_tokens: 0, output_tokens: 0, calls: 0,
    estimated_calls: 0, unpriced_calls: 0, cache_hit_rate: 0.0, cache_savings_usd: 0.0,
  },
  buckets: [
    {
      start: '2026-09-01', end: '2026-09-01', partial: false,
      cost_usd: 0.0,
      by_key: { 'opus5.0': { cost_usd: 0.0, prompt_tokens: 0, output_tokens: 0, calls: 0 } },
    },
  ],
  rows: [
    {
      key: 'opus5.0', label: 'Opus 5', cost_usd: 0.0, share: 0.0,
      prompt_tokens: 0, cached_pct: 0.0, output_tokens: 0, calls: 0,
      estimated_calls: 0, note: '', deleted: false,
    },
  ],
};

describe('UsageResponseSchema', () => {
  it('accepts the contract example and the test fixture', () => {
    expect(UsageResponseSchema.safeParse(CONTRACT_EXAMPLE).success).toBe(true);
    expect(UsageResponseSchema.safeParse(usageFixture()).success).toBe(true);
  });

  it('keeps every field the tab reads (nested objects would otherwise drop them)', () => {
    const parsed = UsageResponseSchema.parse(usageFixture());
    const sol = parsed.rows.find((r) => r.key === 'gpt5.6-sol')!;
    expect(sol.note).toMatch(/list rates/);
    expect(sol.estimated_calls).toBe(3);
    expect(parsed.buckets[0].partial).toBe(true);
    expect(Object.keys(parsed.buckets[0].by_key)).toEqual(['gpt5.6-sol', 'opus5.0']);
  });

  it('rejects totals without the unpriced call count the chart message reads', () => {
    const { unpriced_calls: _unpriced, ...totals } = CONTRACT_EXAMPLE.totals;
    expect(UsageResponseSchema.safeParse({ ...CONTRACT_EXAMPLE, totals }).success).toBe(false);
  });

  it('rejects the retired /daily shape instead of reading it as empty', () => {
    expect(UsageResponseSchema.safeParse({ days: [] }).success).toBe(false);
    expect(UsageResponseSchema.safeParse({ daily: [] }).success).toBe(false);
  });

  it('rejects a row without its pricing note or estimate count', () => {
    const { note: _note, ...noNote } = CONTRACT_EXAMPLE.rows[0];
    expect(UsageResponseSchema.safeParse({ ...CONTRACT_EXAMPLE, rows: [noNote] }).success).toBe(false);
    const { estimated_calls: _est, ...noEst } = CONTRACT_EXAMPLE.rows[0];
    expect(UsageResponseSchema.safeParse({ ...CONTRACT_EXAMPLE, rows: [noEst] }).success).toBe(false);
  });

  it('catches a fraction sent as a percentage and the reverse', () => {
    const row = CONTRACT_EXAMPLE.rows[0];
    // share is a [0, 1] fraction; cached_pct is a [0, 100] percentage.
    expect(UsageResponseSchema.safeParse({ ...CONTRACT_EXAMPLE, rows: [{ ...row, share: 45 }] }).success).toBe(false);
    expect(UsageResponseSchema.safeParse({ ...CONTRACT_EXAMPLE, rows: [{ ...row, cached_pct: 150 }] }).success).toBe(false);
    expect(
      UsageResponseSchema.safeParse({
        ...CONTRACT_EXAMPLE,
        totals: { ...CONTRACT_EXAMPLE.totals, cache_hit_rate: 90.8 },
      }).success,
    ).toBe(false);
  });

  it('rejects a report that is not in UTC days', () => {
    expect(
      UsageResponseSchema.safeParse({
        ...CONTRACT_EXAMPLE,
        range: { ...CONTRACT_EXAMPLE.range, timezone: 'Europe/Warsaw' },
      }).success,
    ).toBe(false);
  });

  it('allows a negative cache saving (writes can cost more than reads save)', () => {
    expect(
      UsageResponseSchema.safeParse({
        ...CONTRACT_EXAMPLE,
        totals: { ...CONTRACT_EXAMPLE.totals, cache_savings_usd: -0.5 },
      }).success,
    ).toBe(true);
  });
});
