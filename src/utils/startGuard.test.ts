import { describe, expect, it, vi } from 'vitest';
import { createStartGuard, StartCancelled } from './startGuard';

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('startGuard', () => {
  it('hands the value back while the attempt is current, releasing nothing', async () => {
    const attempt = createStartGuard().begin();
    const release = vi.fn();
    await expect(attempt.hold(Promise.resolve('mic'), release)).resolves.toBe('mic');
    expect(release).not.toHaveBeenCalled();
    expect(attempt.current).toBe(true);
  });

  it('releases what an await brought in after a cancel, and unwinds the start', async () => {
    const guard = createStartGuard();
    const attempt = guard.begin();
    const pending = deferred<string>();
    const release = vi.fn();
    const held = attempt.hold(pending.promise, release);

    guard.cancel();
    pending.resolve('mic');

    await expect(held).rejects.toBeInstanceOf(StartCancelled);
    expect(release).toHaveBeenCalledWith('mic');
    expect(attempt.current).toBe(false);
  });

  it('a rejection after a cancel is the cancel, not an error of a live start', async () => {
    const guard = createStartGuard();
    const attempt = guard.begin();
    const pending = deferred<void>();
    const held = attempt.hold(pending.promise);
    guard.cancel();
    pending.reject(new Error('socket closed'));
    await expect(held).rejects.toBeInstanceOf(StartCancelled);
  });

  it('a live start still sees its own errors', async () => {
    const attempt = createStartGuard().begin();
    await expect(attempt.hold(Promise.reject(new Error('denied')))).rejects.toThrow('denied');
  });

  it('a newer attempt replaces the older one', async () => {
    const guard = createStartGuard();
    const older = guard.begin();
    const newer = guard.begin();
    expect(older.current).toBe(false);
    expect(newer.current).toBe(true);
    await expect(older.hold(Promise.resolve(1))).rejects.toBeInstanceOf(StartCancelled);
  });
});
