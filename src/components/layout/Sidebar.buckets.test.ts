import { describe, expect, it } from 'vitest';
import { bucketFor, bucketForSession } from './Sidebar';

// Fixed reference "now": Wednesday 2026-06-17, 15:00 local time.
const NOW = new Date(2026, 5, 17, 15, 0, 0);

function daysAgo(n: number, hour = 9): string {
  const d = new Date(2026, 5, 17 - n, hour, 30, 0);
  return d.toISOString();
}

describe('bucketFor date windows', () => {
  it('same calendar day is Today regardless of hour', () => {
    expect(bucketFor(daysAgo(0, 0), NOW)).toBe('Today');
    expect(bucketFor(daysAgo(0, 23), NOW)).toBe('Today');
  });

  it('previous calendar day is Yesterday', () => {
    expect(bucketFor(daysAgo(1, 0), NOW)).toBe('Yesterday');
    expect(bucketFor(daysAgo(1, 23), NOW)).toBe('Yesterday');
  });

  it('days 2 through 7 are Last week', () => {
    expect(bucketFor(daysAgo(2), NOW)).toBe('Last week');
    expect(bucketFor(daysAgo(7), NOW)).toBe('Last week');
  });

  it('days 8 through 30 are Last month', () => {
    expect(bucketFor(daysAgo(8), NOW)).toBe('Last month');
    expect(bucketFor(daysAgo(30), NOW)).toBe('Last month');
  });

  it('day 31 and beyond is Older', () => {
    expect(bucketFor(daysAgo(31), NOW)).toBe('Older');
    expect(bucketFor(daysAgo(365), NOW)).toBe('Older');
  });

  it('windows are calendar-day based, not 24h-multiples', () => {
    // 7 days ago at 23:59 is still "Last week" even though it is more
    // than 7*24h before NOW's morning... and vice versa: the bucket only
    // depends on the calendar date.
    const lateSevenDaysAgo = new Date(2026, 5, 10, 23, 59, 0).toISOString();
    const earlySevenDaysAgo = new Date(2026, 5, 10, 0, 1, 0).toISOString();
    expect(bucketFor(lateSevenDaysAgo, NOW)).toBe('Last week');
    expect(bucketFor(earlySevenDaysAgo, NOW)).toBe('Last week');
  });

  it('invalid dates fall into Older', () => {
    expect(bucketFor('', NOW)).toBe('Older');
    expect(bucketFor('not-a-date', NOW)).toBe('Older');
  });
});

describe('bucketForSession flag routing', () => {
  const date = daysAgo(0);

  it('archived beats pinned beats date', () => {
    expect(bucketForSession({ date, pinned: true, archived: true }, NOW)).toBe('Archived');
    expect(bucketForSession({ date, pinned: true, archived: false }, NOW)).toBe('Pinned');
    expect(bucketForSession({ date: daysAgo(60), archived: true }, NOW)).toBe('Archived');
  });

  it('unflagged sessions use the date windows', () => {
    expect(bucketForSession({ date: daysAgo(0) }, NOW)).toBe('Today');
    expect(bucketForSession({ date: daysAgo(15) }, NOW)).toBe('Last month');
  });
});
