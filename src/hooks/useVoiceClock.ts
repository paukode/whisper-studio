import { useEffect, useState } from 'react';
import { useVoiceStore } from '@/stores/voiceStore';

/** Bedrock closes a Sonic stream after 8 minutes; the server renews it earlier. */
const RENEW_AFTER_MS = 390_000;

/** Elapsed conversation time and the countdown to the next stream renewal,
 *  ticking once a second while voice mode is on. */
export function useVoiceClock(): { elapsed: string; renewIn: string | null } {
  const status = useVoiceStore((s) => s.status);
  const startedAt = useVoiceStore((s) => s.startedAt);
  const streamStartedAt = useVoiceStore((s) => s.streamStartedAt);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (status === 'off') return;
    // Tick from the interval only (no synchronous setState in the effect body);
    // the first paint shows 00:00 and catches up within a second.
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [status]);

  const elapsed = startedAt ? fmt(Math.max(0, now - startedAt)) : '00:00';
  const renewIn = streamStartedAt ? fmt(Math.max(0, RENEW_AFTER_MS - (now - streamStartedAt))) : null;
  return { elapsed, renewIn };
}

function fmt(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
