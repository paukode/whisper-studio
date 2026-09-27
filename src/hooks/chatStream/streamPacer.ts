/**
 * Paces streamed text onto the screen: at most one store write per frame,
 * and a clump of tokens revealed as an even flow instead of a jump.
 *
 * Measured 2026-09-25 with GPT-6 Sol on Bedrock: the answer arrives in clumps
 * of 16 to 25 tokens every ~215 ms, well under a millisecond apart inside a
 * clump, so painting on arrival showed the reply in five jumps a second. A
 * store write per token also cost a render per token.
 *
 * - The first write of a stream is immediate: pacing never delays the first
 *   token on screen.
 * - After that, each frame reveals a share of the backlog (a few characters
 *   at least, all of it when small), so a clump drains in about DRAIN_MS and
 *   the screen trails the network by no more than that.
 * - A backlog too large to animate is written whole.
 * - A hidden window runs no animation frames: text is written as it arrives,
 *   and a timer stands in for a frame that never comes.
 * - flush() writes what is pending now; reset() drops it, for a caller that
 *   has already committed the full text, and starts over.
 */

const DRAIN_MS = 120;
const MIN_CHARS_PER_FRAME = 3;
const MAX_BACKLOG_CHARS = 4000;
const FRAME_FALLBACK_MS = 100;
const FIRST_FRAME_MS = 16;

export interface StreamPacer {
  push(text: string): void;
  flush(): void;
  reset(): void;
}

export function createStreamPacer(write: (text: string) => void): StreamPacer {
  let pending = '';
  let started = false;
  let raf = 0;
  let fallback: ReturnType<typeof setTimeout> | null = null;
  // Time of the previous frame of the current drain; 0 while idle, so a new
  // clump starts its drain from one frame's share instead of all at once.
  let lastFrame = 0;

  const cancel = () => {
    if (raf) cancelAnimationFrame(raf);
    if (fallback !== null) clearTimeout(fallback);
    raf = 0;
    fallback = null;
  };

  const writeAll = () => {
    cancel();
    lastFrame = 0;
    if (!pending) return;
    const out = pending;
    pending = '';
    write(out);
  };

  const frame = (now: number) => {
    raf = 0;
    if (fallback !== null) clearTimeout(fallback);
    fallback = null;
    if (!pending) {
      lastFrame = 0;
      return;
    }
    const dt = lastFrame ? Math.min(now - lastFrame, DRAIN_MS) : FIRST_FRAME_MS;
    lastFrame = now;
    let n = Math.min(
      pending.length,
      Math.max(MIN_CHARS_PER_FRAME, Math.ceil((pending.length * dt) / DRAIN_MS)),
    );
    // Never end a write inside a surrogate pair: half an emoji would paint as
    // a broken glyph for a frame.
    const last = pending.charCodeAt(n - 1);
    if (last >= 0xd800 && last <= 0xdbff && n < pending.length) n += 1;
    const out = pending.slice(0, n);
    pending = pending.slice(n);
    write(out);
    if (pending) schedule();
    else lastFrame = 0;
  };

  const schedule = () => {
    if (raf) return;
    if (typeof requestAnimationFrame !== 'function') {
      writeAll();
      return;
    }
    raf = requestAnimationFrame(frame);
    fallback = setTimeout(writeAll, FRAME_FALLBACK_MS);
  };

  const hidden = () => typeof document !== 'undefined' && document.hidden;

  return {
    push(text: string) {
      if (!text) return;
      pending += text;
      if (!started || hidden() || pending.length > MAX_BACKLOG_CHARS) {
        started = true;
        writeAll();
        return;
      }
      schedule();
    },
    flush: writeAll,
    reset() {
      cancel();
      lastFrame = 0;
      pending = '';
      // What follows is a new segment: its first text paints at once too.
      started = false;
    },
  };
}
