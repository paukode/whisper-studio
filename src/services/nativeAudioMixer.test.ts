/**
 * NativeAudioMixer — the mic-clocked mix buffer between native (shell)
 * capture chunks and mic worklet frames.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NativeAudioMixer } from './nativeAudioMixer';

const frame = (n: number, fill = 0): Float32Array => new Float32Array(n).fill(fill);

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('NativeAudioMixer', () => {
  it('sums native samples into the mic frame', () => {
    const mixer = new NativeAudioMixer();
    mixer.push(new Float32Array([0.25, -0.25, 0.5]));
    const out = mixer.mixInto(new Float32Array([0.1, 0.1, -0.1]));
    expect(out[0]).toBeCloseTo(0.35, 5);
    expect(out[1]).toBeCloseTo(-0.15, 5);
    expect(out[2]).toBeCloseTo(0.4, 5);
    expect(mixer.buffered).toBe(0);
  });

  it('clamps the mixed signal to [-1, 1]', () => {
    const mixer = new NativeAudioMixer();
    mixer.push(new Float32Array([0.9, -0.9]));
    const out = mixer.mixInto(new Float32Array([0.5, -0.5]));
    expect(out[0]).toBe(1);
    expect(out[1]).toBe(-1);
  });

  it('pads silence on underrun (mic frame passes through unchanged)', () => {
    const mixer = new NativeAudioMixer();
    mixer.push(new Float32Array([0.5, 0.5]));
    const out = mixer.mixInto(new Float32Array([0.1, 0.1, 0.1, 0.1]));
    expect(out[0]).toBeCloseTo(0.6, 5);
    expect(out[1]).toBeCloseTo(0.6, 5);
    // No native samples left for these — the mic samples are untouched.
    expect(out[2]).toBeCloseTo(0.1, 5);
    expect(out[3]).toBeCloseTo(0.1, 5);
    expect(mixer.buffered).toBe(0);
  });

  it('an empty buffer leaves the whole frame untouched', () => {
    const mixer = new NativeAudioMixer();
    const out = mixer.mixInto(new Float32Array([0.3, -0.3]));
    expect(Array.from(out)).toEqual([Float32Array.from([0.3])[0], Float32Array.from([-0.3])[0]]);
  });

  it('spans chunk boundaries across consecutive mic frames', () => {
    const mixer = new NativeAudioMixer();
    mixer.push(new Float32Array([0.1, 0.2, 0.3]));
    mixer.push(new Float32Array([0.4, 0.5]));
    const first = mixer.mixInto(frame(2));
    expect(first[0]).toBeCloseTo(0.1, 5);
    expect(first[1]).toBeCloseTo(0.2, 5);
    const second = mixer.mixInto(frame(2));
    expect(second[0]).toBeCloseTo(0.3, 5);
    expect(second[1]).toBeCloseTo(0.4, 5);
    expect(mixer.buffered).toBe(1);
  });

  it('drops the OLDEST samples once the buffer exceeds its cap, logging once', () => {
    const mixer = new NativeAudioMixer(4);
    mixer.push(new Float32Array([0.1, 0.2, 0.3]));
    mixer.push(new Float32Array([0.4, 0.5, 0.6]));
    // Cap 4: the two oldest (0.1, 0.2) were dropped.
    expect(mixer.buffered).toBe(4);
    const out = mixer.mixInto(frame(4));
    expect(out[0]).toBeCloseTo(0.3, 5);
    expect(out[1]).toBeCloseTo(0.4, 5);
    expect(out[2]).toBeCloseTo(0.5, 5);
    expect(out[3]).toBeCloseTo(0.6, 5);
    expect(console.warn).toHaveBeenCalledTimes(1);
    // Overflow again — still only the single log.
    mixer.push(new Float32Array(10));
    expect(console.warn).toHaveBeenCalledTimes(1);
  });

  it('reset clears buffered samples and re-arms the overflow log', () => {
    const mixer = new NativeAudioMixer(2);
    mixer.push(new Float32Array([1, 2, 3]));
    expect(console.warn).toHaveBeenCalledTimes(1);
    mixer.reset();
    expect(mixer.buffered).toBe(0);
    mixer.push(new Float32Array([1, 2, 3]));
    expect(console.warn).toHaveBeenCalledTimes(2);
  });
});
