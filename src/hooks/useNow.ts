import { useEffect, useState } from 'react';

/** The current time (ms epoch), refreshed every `intervalMs` while `active`:
 *  what elapsed readouts and countdowns tick on. */
export function useNow(intervalMs: number, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, active]);
  return now;
}
