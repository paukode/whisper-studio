import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { msToNextUtcMidnight, useUtcNow } from './useUtcNow';
import { utcToday } from './usageView';

afterEach(() => {
  vi.useRealTimers();
});

describe('msToNextUtcMidnight', () => {
  it('counts to the next 00:00 UTC, a whole day at midnight itself', () => {
    expect(msToNextUtcMidnight(new Date('2026-09-23T23:59:00Z'))).toBe(60_000);
    expect(msToNextUtcMidnight(new Date('2026-09-24T00:00:00Z'))).toBe(86_400_000);
    // Month and year ends roll over on the UTC calendar.
    expect(msToNextUtcMidnight(new Date('2026-12-31T23:00:00Z'))).toBe(3_600_000);
  });
});

describe('useUtcNow', () => {
  it('keeps one moment for the whole UTC day and moves to the next day at 00:00 UTC', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-23T23:59:00Z'));
    const { result, unmount } = renderHook(() => useUtcNow());
    const opened = result.current;
    expect(utcToday(opened)).toBe('2026-09-23');

    act(() => vi.advanceTimersByTime(59_000));
    expect(result.current).toBe(opened);

    act(() => vi.advanceTimersByTime(1_000));
    expect(utcToday(result.current)).toBe('2026-09-24');

    // And again a day later: the timer is armed for every new day.
    act(() => vi.advanceTimersByTime(86_400_000));
    expect(utcToday(result.current)).toBe('2026-09-25');
    unmount();
  });

  it('catches up on focus or visibility after a sleep the timer did not cover', () => {
    // Only the clock is faked: the real midnight timer never fires in the test,
    // as it may not on a Mac that slept through midnight.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-23T22:00:00Z'));
    const { result, unmount } = renderHook(() => useUtcNow());
    const opened = result.current;

    // A focus on the same day changes nothing.
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    expect(result.current).toBe(opened);

    vi.setSystemTime(new Date('2026-09-24T07:00:00Z'));
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    expect(utcToday(result.current)).toBe('2026-09-24');

    vi.setSystemTime(new Date('2026-09-25T07:00:00Z'));
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(utcToday(result.current)).toBe('2026-09-25');
    unmount();
  });
});
