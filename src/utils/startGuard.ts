/**
 * Start guard: cancellation for an async start that acquires its resources
 * across several awaits (a socket, the microphone, a native capture).
 *
 * A Stop can land while the start is still awaiting one of them. The start
 * then must not go on to activate a session, and whatever the pending await
 * hands back after the Stop has nobody left to release it. So each start
 * takes an attempt from begin(), Stop (or an unmount) calls cancel(), and
 * every await of the start goes through attempt.hold(). While the attempt is
 * current, hold() returns what the await resolved to; once it was cancelled,
 * hold() passes that value to `release` and throws StartCancelled, and the
 * start unwinds without touching shared state. Whatever the start held
 * before the await, the canceller already let go of on its own stop path,
 * so `release` covers only what the await itself brought into being.
 *
 * Used by the dictation hook (useChatInputMic) and the meeting recorder
 * (recordingController).
 */

/** Thrown by StartAttempt.hold() once the attempt was cancelled. */
export class StartCancelled extends Error {
  constructor() {
    super('The start was cancelled.');
    this.name = 'StartCancelled';
  }
}

export interface StartAttempt {
  /** False once cancel() or a newer begin() replaced this attempt. */
  readonly current: boolean;
  /** Await `pending`. If the attempt was cancelled meanwhile, give what it
   *  resolved to to `release` and throw StartCancelled (a rejection after a
   *  cancel also becomes StartCancelled: it is no error of a live start). */
  hold<T>(pending: Promise<T>, release?: (value: T) => void): Promise<T>;
}

export interface StartGuard {
  /** Begin a start attempt. Any attempt still in flight is cancelled. */
  begin(): StartAttempt;
  /** Cancel the attempt in flight, if any. */
  cancel(): void;
}

export function createStartGuard(): StartGuard {
  let generation = 0;
  return {
    begin() {
      const mine = ++generation;
      return {
        get current() {
          return generation === mine;
        },
        async hold<T>(pending: Promise<T>, release?: (value: T) => void): Promise<T> {
          let value: T;
          try {
            value = await pending;
          } catch (err) {
            if (generation !== mine) throw new StartCancelled();
            throw err;
          }
          if (generation !== mine) {
            release?.(value);
            throw new StartCancelled();
          }
          return value;
        },
      };
    },
    cancel() {
      generation += 1;
    },
  };
}
