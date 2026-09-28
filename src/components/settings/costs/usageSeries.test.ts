import { describe, expect, it } from 'vitest';
import { OTHER_KEY, TOP_SERIES, bucketSegments, chartSeries, rowName } from './usageSeries';
import { usageFixture, usageFor } from '@/test/fixtures/costsUsage';
import type { UsageBucket, UsageRow } from '@/types/costs';

const byLabel = (row: UsageRow) => row.label;

describe('chartSeries', () => {
  it('gives the top keys by cost their own colour and stacks the rest as Other', () => {
    const report = usageFixture();
    const series = chartSeries(report.rows, report.buckets, byLabel);
    expect(series).toHaveLength(TOP_SERIES + 1);
    expect(series[series.length - 1]).toMatchObject({ key: OTHER_KEY, label: 'Other', slot: 'other' });
    const named = series.slice(0, TOP_SERIES).map((s) => s.key);
    const cheapestNamed = Math.min(
      ...report.rows.filter((r) => named.includes(r.key)).map((r) => r.cost_usd),
    );
    for (const r of report.rows.filter((row) => !named.includes(row.key))) {
      expect(r.cost_usd).toBeLessThanOrEqual(cheapestNamed);
    }
  });

  it('marks a series estimated when its rows carry estimated calls', () => {
    const report = usageFixture();
    const series = chartSeries(report.rows, report.buckets, byLabel);
    const estimated = Object.fromEntries(series.map((s) => [s.key, s.estimated]));
    // gpt5.6-sol has 3 estimated calls; the on-device model (in Other) has 5.
    expect(estimated).toEqual({
      'gpt5.6-sol': true,
      'gpt6-astra': false,
      'opus5.0': false,
      sonnet5: false,
      'haiku4.5': false,
      [OTHER_KEY]: true,
    });
  });

  it('adds no Other when every key has its own colour', () => {
    const report = usageFor('/api/costs/usage?split=source');
    const series = chartSeries(report.rows, report.buckets, byLabel);
    expect(series.map((s) => s.key)).toEqual(['chat', 'agent', 'voice']);
  });

  it('names two keys that share a name by their raw keys too', () => {
    const rows: UsageRow[] = ['sess-a', 'sess-b', 'live'].map((key, i) => ({
      key,
      label: key === 'live' ? 'Live chat' : key,
      cost_usd: 3 - i,
      share: 0,
      prompt_tokens: 0,
      cached_pct: 0,
      output_tokens: 0,
      calls: 1,
      estimated_calls: 0,
      note: '',
      deleted: key !== 'live',
    }));
    const series = chartSeries(rows, [], (r) => rowName(r, 'session', () => undefined));
    expect(series.map((s) => s.label)).toEqual([
      'Deleted session (sess-a)',
      'Deleted session (sess-b)',
      'Live chat',
    ]);
  });

  it('adds Other when a bucket holds a key the rows do not rank', () => {
    const rows = usageFor('/api/costs/usage?split=source').rows;
    const buckets: UsageBucket[] = [
      {
        start: '2026-09-01',
        end: '2026-09-01',
        partial: false,
        cost_usd: 1,
        by_key: { stray: { cost_usd: 1, prompt_tokens: 1, output_tokens: 1, calls: 1 } },
      },
    ];
    const series = chartSeries(rows, buckets, byLabel);
    expect(series[series.length - 1].key).toBe(OTHER_KEY);
  });
});

describe('bucketSegments', () => {
  it('the stacked segments of every bucket add up to that bucket', () => {
    const report = usageFixture();
    const series = chartSeries(report.rows, report.buckets, byLabel);
    for (const bucket of report.buckets) {
      const segs = bucketSegments(bucket, series);
      const sum = (f: 'cost_usd' | 'prompt_tokens' | 'output_tokens' | 'calls') =>
        segs.reduce((n, s) => n + s[f], 0);
      const want = (f: 'cost_usd' | 'prompt_tokens' | 'output_tokens' | 'calls') =>
        Object.values(bucket.by_key).reduce((n, k) => n + k[f], 0);
      expect(sum('cost_usd')).toBeCloseTo(bucket.cost_usd, 6);
      expect(sum('prompt_tokens')).toBe(want('prompt_tokens'));
      expect(sum('output_tokens')).toBe(want('output_tokens'));
      expect(sum('calls')).toBe(want('calls'));
    }
  });
});

describe('rowName', () => {
  const base: UsageRow = {
    key: 'k',
    label: 'k',
    cost_usd: 0,
    share: 0,
    prompt_tokens: 0,
    cached_pct: 0,
    output_tokens: 0,
    calls: 0,
    estimated_calls: 0,
    note: '',
    deleted: false,
  };
  const picker = (key: string) => (key === 'k' ? 'Picker name' : undefined);

  it('a deleted session the server cannot name reads "Deleted session"', () => {
    expect(rowName({ ...base, deleted: true }, 'session', picker)).toBe('Deleted session');
    expect(rowName({ ...base, label: '', deleted: true }, 'session', picker)).toBe('Deleted session');
  });

  it('an id that never was a saved session keeps the label the server gives it', () => {
    const dream = { ...base, key: 'dream', label: 'Memory dream (not a saved session)', deleted: true };
    expect(rowName(dream, 'session', picker)).toBe('Memory dream (not a saved session)');
  });

  it('the server label wins; the picker names a model the server could not', () => {
    expect(rowName({ ...base, label: 'Opus 5' }, 'model', picker)).toBe('Opus 5');
    expect(rowName(base, 'model', picker)).toBe('Picker name');
    expect(rowName({ ...base, key: 'x', label: 'x' }, 'model', picker)).toBe('x');
    // The picker only names models.
    expect(rowName(base, 'source', picker)).toBe('k');
  });
});
