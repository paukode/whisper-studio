/**
 * The wait clock: the parts of a turn when the model was not thinking. An
 * approval card or a question waits on the user, and a sleep tool waits on
 * the clock. The "Thinking" timer counts only the time outside these spans,
 * and the waits show on their own ("Thinking 41.0s · waited on you 18m").
 *
 * Times are performance.now() values, the same clock as thinkingStartTime.
 * Every function here is pure: the chat store holds the spans and the open
 * reasons, and the timer reads them.
 */

/** 'user': an approval card or a question waits on the user. 'sleep': a
 *  sleep tool runs. */
export type WaitKind = 'user' | 'sleep';

export interface WaitSpan {
  kind: WaitKind;
  start: number;
  /** null while the wait is still open. */
  end: number | null;
}

export interface WaitState {
  waitSpans: WaitSpan[];
  /** The open reasons, by id (an approval, a tool call), and their kind.
   *  A kind's span stays open while any reason of that kind is open. */
  waitReasons: Record<string, WaitKind>;
}

/** Tools that wait instead of work: the time they run is not thinking. */
const WAIT_TOOLS: Record<string, WaitKind> = {
  sleep: 'sleep',
  ask_user_question: 'user',
};

/** The wait kind of a tool, or null for a tool that does work. */
export function waitKindOfTool(toolName: string): WaitKind | null {
  return WAIT_TOOLS[toolName] ?? null;
}

/** Only the wait fields: the store spreads the result into its update, so
 *  handing back the whole state would undo the rest of that update. */
const pick = (state: WaitState): WaitState => ({ waitSpans: state.waitSpans, waitReasons: state.waitReasons });

const kindOpen = (reasons: Record<string, WaitKind>, kind: WaitKind) =>
  Object.values(reasons).includes(kind);

/** Open a wait for `reason`. A second reason of a kind that is already
 *  open joins its span, so two overlapping waits count once. */
export function beginWait(state: WaitState, reason: string, kind: WaitKind, now: number): WaitState {
  if (state.waitReasons[reason] === kind) return pick(state);
  const spans = kindOpen(state.waitReasons, kind)
    ? state.waitSpans
    : [...state.waitSpans, { kind, start: now, end: null }];
  return { waitSpans: spans, waitReasons: { ...state.waitReasons, [reason]: kind } };
}

/** Close the wait for `reason`. The span ends when no other reason of its
 *  kind is still open. An unknown reason changes nothing. */
export function endWait(state: WaitState, reason: string, now: number): WaitState {
  const kind = state.waitReasons[reason];
  if (!kind) return pick(state);
  const reasons = { ...state.waitReasons };
  delete reasons[reason];
  if (kindOpen(reasons, kind)) return { waitSpans: state.waitSpans, waitReasons: reasons };
  const spans = state.waitSpans.map((s) => (s.kind === kind && s.end === null ? { ...s, end: now } : s));
  return { waitSpans: spans, waitReasons: reasons };
}

/** Close every open wait (the stream ended). */
export function endAllWaits(state: WaitState, now: number): WaitState {
  if (Object.keys(state.waitReasons).length === 0) return pick(state);
  return {
    waitSpans: state.waitSpans.map((s) => (s.end === null ? { ...s, end: now } : s)),
    waitReasons: {},
  };
}

/** The merged intervals of the spans (of one kind, or all), clipped to
 *  [from, now]. Overlapping spans count once. */
function covered(spans: readonly WaitSpan[], from: number, now: number, kind?: WaitKind): number {
  const parts = spans
    .filter((s) => kind === undefined || s.kind === kind)
    .map((s) => [Math.max(from, s.start), Math.min(now, s.end ?? now)] as const)
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  let total = 0;
  let curStart = -Infinity;
  let curEnd = -Infinity;
  for (const [a, b] of parts) {
    if (a > curEnd) {
      if (curEnd > curStart) total += curEnd - curStart;
      curStart = a;
      curEnd = b;
    } else if (b > curEnd) {
      curEnd = b;
    }
  }
  if (curEnd > curStart) total += curEnd - curStart;
  return total;
}

/** Thinking time since `from`: the time in [from, now] outside every wait. */
export function activeMs(from: number, now: number, spans: readonly WaitSpan[]): number {
  return Math.max(0, now - from - covered(spans, from, now));
}

/** Time in [from, now] spent in waits of one kind. */
export function waitedMs(from: number, now: number, spans: readonly WaitSpan[], kind: WaitKind): number {
  return covered(spans, from, now, kind);
}

/** "41s", "18m", "1h 05m": a wait reads in whole units. */
export function formatWait(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

/** The waits shown next to the thinking time; '' when there were none (a
 *  wait under one second does not show). */
export function waitSummary(from: number, now: number, spans: readonly WaitSpan[]): string {
  const parts: string[] = [];
  const user = waitedMs(from, now, spans, 'user');
  const sleep = waitedMs(from, now, spans, 'sleep');
  if (user >= 1000) parts.push(`waited on you ${formatWait(user)}`);
  if (sleep >= 1000) parts.push(`slept ${formatWait(sleep)}`);
  return parts.join(' · ');
}
