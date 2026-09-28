/**
 * The pacer between the stream and the screen: the first token is never
 * delayed, a clump is revealed over several frames in order and in full,
 * and nothing is lost on flush, reset, or a hidden window.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createStreamPacer } from './streamPacer';

const FAKED = ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame', 'performance'] as const;

describe('createStreamPacer', () => {
  let writes: string[];
  const pacer = () => createStreamPacer((t) => writes.push(t));

  beforeEach(() => {
    writes = [];
    vi.useFakeTimers({ toFake: [...FAKED] });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('writes the first text at once', () => {
    const p = pacer();
    p.push('Dear');
    expect(writes).toEqual(['Dear']);
  });

  it('reveals a clump over several frames, in order and complete', () => {
    const p = pacer();
    p.push('A');
    const clump = 'x'.repeat(40) + 'y'.repeat(40) + 'z'.repeat(40);
    p.push(clump);
    expect(writes).toEqual(['A']);
    vi.advanceTimersByTime(16);
    expect(writes.length).toBe(2);
    expect(writes[1].length).toBeLessThan(clump.length);
    vi.advanceTimersByTime(1000);
    expect(writes.length).toBeGreaterThan(3);
    expect(writes.join('')).toBe('A' + clump);
  });

  it('writes at most once per frame however many tokens arrive', () => {
    const p = pacer();
    p.push('first ');
    for (let i = 0; i < 50; i++) p.push('t ');
    vi.advanceTimersByTime(16);
    expect(writes.length).toBe(2);
  });

  it('flush writes everything pending now', () => {
    const p = pacer();
    p.push('one ');
    p.push('two three four five six seven eight nine ten');
    p.flush();
    expect(writes.join('')).toBe('one two three four five six seven eight nine ten');
    vi.advanceTimersByTime(1000);
    expect(writes.join('')).toBe('one two three four five six seven eight nine ten');
  });

  it('reset drops pending text and paints the next segment at once', () => {
    const p = pacer();
    p.push('seg one ');
    p.push('already committed elsewhere');
    p.reset();
    vi.advanceTimersByTime(1000);
    expect(writes).toEqual(['seg one ']);
    p.push('seg two');
    expect(writes).toEqual(['seg one ', 'seg two']);
  });

  it('writes as text arrives in a hidden window', () => {
    const p = pacer();
    p.push('a');
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    p.push('b');
    p.push('c');
    expect(writes).toEqual(['a', 'b', 'c']);
  });

  it('lands the text when no animation frame comes', () => {
    const p = pacer();
    p.push('a');
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1);
    p.push('rest of the reply');
    vi.advanceTimersByTime(150);
    expect(writes.join('')).toBe('arest of the reply');
  });

  it('never splits an emoji across writes', () => {
    const p = pacer();
    p.push('a');
    p.push('\u{1F600}'.repeat(30));
    vi.advanceTimersByTime(1000);
    for (const w of writes) {
      const last = w.charCodeAt(w.length - 1);
      expect(last >= 0xd800 && last <= 0xdbff).toBe(false);
    }
    expect(writes.join('')).toBe('a' + '\u{1F600}'.repeat(30));
  });
});
