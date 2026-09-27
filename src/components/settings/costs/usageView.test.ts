import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ALL_TIME_FROM,
  DEFAULT_VIEW,
  addDays,
  bucketLabel,
  exportUrl,
  fmtMoney,
  isIsoDate,
  loadCostsView,
  presetRange,
  resolveRange,
  saveCostsView,
  usageUrl,
  utcToday,
} from './usageView';
import { STORAGE_KEYS } from '@/utils/storageKeys';

afterEach(() => {
  vi.unstubAllEnvs();
  localStorage.clear();
});

describe('UTC days', () => {
  it('"today" is the UTC day even where the local day has already rolled over', () => {
    vi.stubEnv('TZ', 'Europe/Warsaw');
    const now = new Date('2026-09-23T22:30:00Z');
    // Guard: the local calendar really is on the next day here.
    expect(now.getDate()).toBe(24);
    expect(utcToday(now)).toBe('2026-09-23');
    expect(presetRange('today', now)).toEqual({ from: '2026-09-23', to: '2026-09-23' });
  });

  it('presets cover whole UTC days, both ends inclusive', () => {
    const now = new Date('2026-09-23T12:00:00Z');
    const days = (r: { from: string; to: string }) =>
      (Date.parse(r.to) - Date.parse(r.from)) / 86_400_000 + 1;
    expect(days(presetRange('last7', now))).toBe(7);
    expect(days(presetRange('last30', now))).toBe(30);
    expect(presetRange('last30', now).to).toBe('2026-09-23');
    expect(presetRange('thisMonth', now)).toEqual({ from: '2026-09-01', to: '2026-09-23' });
    expect(presetRange('lastMonth', now)).toEqual({ from: '2026-08-01', to: '2026-08-31' });
    expect(presetRange('all', now)).toEqual({ from: ALL_TIME_FROM, to: '2026-09-23' });
  });

  it('last month crosses a year boundary and knows short months', () => {
    expect(presetRange('lastMonth', new Date('2026-01-15T00:00:00Z'))).toEqual({
      from: '2025-12-01',
      to: '2025-12-31',
    });
    expect(presetRange('lastMonth', new Date('2028-03-01T00:00:00Z'))).toEqual({
      from: '2028-02-01',
      to: '2028-02-29',
    });
  });

  it('addDays and isIsoDate work on the calendar, not on local time', () => {
    vi.stubEnv('TZ', 'America/Los_Angeles');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01');
    expect(isIsoDate('2026-02-29')).toBe(false);
    expect(isIsoDate('2028-02-29')).toBe(true);
    expect(isIsoDate('2026-9-1')).toBe(false);
  });
});

describe('custom ranges', () => {
  const now = new Date('2026-09-23T12:00:00Z');

  it('asks for both dates before it requests anything', () => {
    const r = resolveRange({ ...DEFAULT_VIEW, range: 'custom', customFrom: '2026-09-01', customTo: '' }, now);
    expect(r).toEqual({ ok: false, reason: 'Pick both a From and a To date.' });
  });

  it('refuses a range that runs backwards', () => {
    const r = resolveRange(
      { ...DEFAULT_VIEW, range: 'custom', customFrom: '2026-09-10', customTo: '2026-09-01' },
      now,
    );
    expect(r).toEqual({ ok: false, reason: 'From must be on or before To.' });
  });

  it('accepts a single day', () => {
    const r = resolveRange(
      { ...DEFAULT_VIEW, range: 'custom', customFrom: '2026-09-10', customTo: '2026-09-10' },
      now,
    );
    expect(r).toEqual({ ok: true, range: { from: '2026-09-10', to: '2026-09-10' } });
  });
});

describe('request URLs', () => {
  it('carry the contract parameter names', () => {
    const range = { from: '2026-09-01', to: '2026-09-23' };
    const usage = new URL(usageUrl(range, 'week', 'source'), 'http://x');
    expect(usage.pathname).toBe('/api/costs/usage');
    expect(Object.fromEntries(usage.searchParams)).toEqual({
      from: '2026-09-01',
      to: '2026-09-23',
      granularity: 'week',
      split: 'source',
    });
    const exp = new URL(exportUrl(range, 'json'), 'http://x');
    expect(exp.pathname).toBe('/api/costs/export');
    expect(Object.fromEntries(exp.searchParams)).toEqual({
      from: '2026-09-01',
      to: '2026-09-23',
      format: 'json',
    });
  });
});

describe('remembered view', () => {
  it('round-trips a saved view', () => {
    const view = { range: 'custom' as const, customFrom: '2026-09-01', customTo: '2026-09-05', granularity: 'month' as const, split: 'session' as const };
    saveCostsView(view);
    expect(loadCostsView()).toEqual(view);
  });

  it('drops junk field by field instead of failing', () => {
    localStorage.setItem(
      STORAGE_KEYS.COSTS_VIEW,
      JSON.stringify({ range: 'forever', customFrom: '2026-13-01', granularity: 'week', split: 42 }),
    );
    expect(loadCostsView()).toEqual({ ...DEFAULT_VIEW, granularity: 'week' });
    localStorage.setItem(STORAGE_KEYS.COSTS_VIEW, '{not json');
    expect(loadCostsView()).toEqual(DEFAULT_VIEW);
  });
});

describe('formatting', () => {
  it('money: two decimals from $1 up, four below', () => {
    expect(fmtMoney(1234.5)).toBe('$1,234.50');
    expect(fmtMoney(1)).toBe('$1.00');
    expect(fmtMoney(0.01234)).toBe('$0.0123');
    expect(fmtMoney(0)).toBe('$0.00');
    expect(fmtMoney(-2.5)).toBe('-$2.50');
  });

  it('a partial bucket is named by its clipped dates', () => {
    expect(bucketLabel({ start: '2026-09-21', end: '2026-09-23', partial: true }, 'week')).toBe(
      'Sep 21 to Sep 23 (partial)',
    );
    expect(bucketLabel({ start: '2026-09-07', end: '2026-09-13', partial: false }, 'week')).toBe(
      'Sep 7 to Sep 13',
    );
    expect(bucketLabel({ start: '2026-08-01', end: '2026-08-31', partial: false }, 'month')).toBe(
      'Aug 2026',
    );
    expect(bucketLabel({ start: '2026-09-01', end: '2026-09-23', partial: true }, 'month')).toBe(
      'Sep 1 to Sep 23 (partial)',
    );
    expect(bucketLabel({ start: '2026-09-05', end: '2026-09-05', partial: false }, 'day')).toBe('Sep 5');
  });
});
