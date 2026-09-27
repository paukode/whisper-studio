import { useEffect, useState } from 'react';
import { utcToday } from './usageView';

/** Milliseconds from `now` to the next 00:00 UTC (a whole day at midnight). */
export function msToNextUtcMidnight(now: Date): number {
  return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1) - now.getTime();
}

/**
 * The moment the Costs tab reads its UTC day from. It stays the same Date for
 * a whole UTC day, so the presets and the "Today so far" hint in one render
 * come from one moment, and it moves to the new day at 00:00 UTC while the tab
 * stays open, the same day the server's daily cap has moved to. A timer fires
 * at the next midnight; a Mac that slept through it catches up when the
 * window regains focus or becomes visible again.
 */
export function useUtcNow(): Date {
  const [now, setNow] = useState(() => new Date());
  const day = utcToday(now);

  useEffect(() => {
    let timer: number | undefined;
    const check = () => {
      const current = new Date();
      if (utcToday(current) !== day) {
        setNow(current);
        return;
      }
      // Still the same day (a timer that fired early, or a focus before
      // midnight): aim again from the current clock.
      window.clearTimeout(timer);
      timer = window.setTimeout(check, msToNextUtcMidnight(current));
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') check();
    };
    timer = window.setTimeout(check, msToNextUtcMidnight(new Date()));
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('focus', check);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [day]);

  return now;
}
