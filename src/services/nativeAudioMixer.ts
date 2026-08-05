/**
 * NativeAudioMixer — buffers native-capture samples between mic worklet
 * frames and sums them into the mic stream.
 *
 * The mic is the clock master: native chunks (16 kHz mono Float32, decoded
 * from the shell bridge) accumulate here, and each mic frame pulls exactly
 * as many buffered samples as its own length. On underrun the native side
 * contributes silence; if the buffer grows past ~500 ms (native producing
 * faster than the mic consumes, e.g. clock drift), the OLDEST samples are
 * dropped so the mixed-in audio stays near-live (logged once per recording).
 */

/** 500 ms at 16 kHz. */
const DEFAULT_MAX_BUFFERED_SAMPLES = 8000;

export class NativeAudioMixer {
  private chunks: Float32Array[] = [];
  /** Total un-consumed samples across `chunks` (past `readOffset`). */
  private length = 0;
  /** Consumed samples within chunks[0]. */
  private readOffset = 0;
  private overflowLogged = false;

  constructor(private readonly maxBufferedSamples = DEFAULT_MAX_BUFFERED_SAMPLES) {}

  /** Un-consumed native samples currently buffered. */
  get buffered(): number {
    return this.length;
  }

  push(samples: Float32Array): void {
    if (samples.length === 0) return;
    this.chunks.push(samples);
    this.length += samples.length;

    let excess = this.length - this.maxBufferedSamples;
    if (excess <= 0) return;
    if (!this.overflowLogged) {
      console.warn(
        `[native-audio] mix buffer exceeded ${this.maxBufferedSamples} samples; dropping oldest audio`,
      );
      this.overflowLogged = true;
    }
    while (excess > 0 && this.chunks.length > 0) {
      const head = this.chunks[0];
      const avail = head.length - this.readOffset;
      if (avail <= excess) {
        this.chunks.shift();
        this.readOffset = 0;
        this.length -= avail;
        excess -= avail;
      } else {
        this.readOffset += excess;
        this.length -= excess;
        excess = 0;
      }
    }
  }

  /**
   * Sum buffered native samples into `micFrame` in place, clamped to [-1, 1].
   * Consumes exactly `micFrame.length` native samples when available; any
   * shortfall mixes silence (the mic frame passes through unchanged there).
   * Returns `micFrame` for convenience.
   */
  mixInto(micFrame: Float32Array): Float32Array {
    let i = 0;
    while (i < micFrame.length && this.chunks.length > 0) {
      const head = this.chunks[0];
      const avail = head.length - this.readOffset;
      const take = Math.min(avail, micFrame.length - i);
      for (let j = 0; j < take; j++) {
        const mixed = micFrame[i + j] + head[this.readOffset + j];
        micFrame[i + j] = mixed > 1 ? 1 : mixed < -1 ? -1 : mixed;
      }
      i += take;
      this.readOffset += take;
      this.length -= take;
      if (this.readOffset >= head.length) {
        this.chunks.shift();
        this.readOffset = 0;
      }
    }
    return micFrame;
  }

  reset(): void {
    this.chunks = [];
    this.length = 0;
    this.readOffset = 0;
    this.overflowLogged = false;
  }
}
