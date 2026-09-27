/**
 * The Costs tab tests render what usageFor() answers, so usageFor() must only
 * ever answer what the contract allows: buckets zero-filled from range.from
 * to range.to, weeks from Monday, partial only where the range clips a week
 * or month, and rows, buckets and totals that add up to one another.
 */
import { describe, expect, it } from 'vitest';
import { UsageResponseSchema } from '@/types/schemas';
import type { UsageGranularity, UsageSplit } from '@/types/costs';
import { usageFixture, usageFor } from './costsUsage';

const url = (from: string, to: string, granularity: UsageGranularity, split: UsageSplit) =>
  `/api/costs/usage?from=${from}&to=${to}&granularity=${granularity}&split=${split}`;

const nextDay = (iso: string) =>
  new Date(Date.parse(`${iso}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);
const weekday = (iso: string) => new Date(`${iso}T00:00:00Z`).getUTCDay();
const isMonthStart = (iso: string) => iso.endsWith('-01');
const isMonthEnd = (iso: string) => nextDay(iso).endsWith('-01');

const RANGES: ReadonlyArray<[string, string]> = [
  ['2026-08-25', '2026-09-23'], // Last 30 days on 2026-09-23: starts on a Tuesday
  ['2026-09-01', '2026-09-23'], // the fixture's own range
  ['2026-09-17', '2026-09-23'], // cuts through the fixture's spend
  ['2026-09-23', '2026-09-23'], // one day
  ['2026-09-14', '2026-09-20'], // one whole week without spend
  ['2025-12-15', '2026-02-10'], // crosses a year, clips both months
  ['2026-07-23', '2026-09-23'], // All time
];
const GRANULARITIES: UsageGranularity[] = ['day', 'week', 'month'];
const SPLITS: UsageSplit[] = ['model', 'session', 'source'];

describe('usageFor', () => {
  it("reproduces the JSON for the fixture's own request", () => {
    const fixture = usageFixture();
    const report = usageFor(url(fixture.range.from, fixture.range.to, fixture.granularity, fixture.split));
    expect(report.range).toEqual(fixture.range);
    expect(report.buckets.map(({ by_key: _k, ...b }) => b)).toEqual(
      fixture.buckets.map(({ by_key: _k, ...b }) => b),
    );
    report.buckets.forEach((b, i) => expect(b.by_key).toEqual(fixture.buckets[i].by_key));
    expect(report.rows.map((r) => r.key)).toEqual(fixture.rows.map((r) => r.key));
    report.rows.forEach((r, i) => {
      const want = fixture.rows[i];
      expect({ ...r, share: 0, cached_pct: 0 }).toEqual({ ...want, share: 0, cached_pct: 0 });
      expect(r.share).toBeCloseTo(want.share, 5);
      expect(r.cached_pct).toBeCloseTo(want.cached_pct, 5);
    });
    for (const [field, want] of Object.entries(fixture.totals)) {
      expect(report.totals[field as keyof typeof fixture.totals]).toBeCloseTo(want, 5);
    }
  });

  for (const [from, to] of RANGES) {
    for (const granularity of GRANULARITIES) {
      for (const split of SPLITS) {
        it(`answers ${from} to ${to} by ${granularity}, split by ${split}, as the contract says`, () => {
          const report = usageFor(url(from, to, granularity, split));
          expect(UsageResponseSchema.safeParse(report).success).toBe(true);
          expect(report).toMatchObject({ range: { from, to, timezone: 'UTC' }, granularity, split });

          const { buckets } = report;
          // Zero-filled over the whole range, with no gap or overlap.
          expect(buckets[0].start).toBe(from);
          expect(buckets[buckets.length - 1].end).toBe(to);
          for (let i = 1; i < buckets.length; i++) {
            expect(buckets[i].start).toBe(nextDay(buckets[i - 1].end));
          }
          buckets.forEach((b, i) => {
            const inner = i > 0 && i < buckets.length - 1;
            if (granularity === 'day') {
              expect(b.start).toBe(b.end);
              expect(b.partial).toBe(false);
              return;
            }
            const wholeStart = granularity === 'week' ? weekday(b.start) === 1 : isMonthStart(b.start);
            const wholeEnd = granularity === 'week' ? weekday(b.end) === 0 : isMonthEnd(b.end);
            if (inner) expect(wholeStart && wholeEnd).toBe(true);
            expect(b.partial).toBe(!(wholeStart && wholeEnd));
          });

          // Buckets, rows and totals add up to one another.
          const keyed = (key: string) =>
            buckets.reduce((n, b) => n + (b.by_key[key]?.cost_usd ?? 0), 0);
          for (const b of buckets) {
            const inside = Object.values(b.by_key).reduce((n, k) => n + k.cost_usd, 0);
            expect(b.cost_usd).toBeCloseTo(inside, 9);
          }
          const bucketKeys = new Set(buckets.flatMap((b) => Object.keys(b.by_key)));
          expect(new Set(report.rows.map((r) => r.key))).toEqual(bucketKeys);
          for (const r of report.rows) expect(r.cost_usd).toBeCloseTo(keyed(r.key), 9);
          const total = report.totals.cost_usd;
          expect(buckets.reduce((n, b) => n + b.cost_usd, 0)).toBeCloseTo(total, 9);
          expect(report.rows.reduce((n, r) => n + r.cost_usd, 0)).toBeCloseTo(total, 9);
          expect(report.rows.reduce((n, r) => n + r.calls, 0)).toBe(report.totals.calls);
          expect(report.rows.reduce((n, r) => n + r.estimated_calls, 0)).toBe(
            report.totals.estimated_calls,
          );
        });
      }
    }
  }
});
