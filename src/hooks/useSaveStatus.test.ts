import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useSaveStatus } from './useSaveStatus';

describe('useSaveStatus', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('starts idle with no message', () => {
    const { result } = renderHook(() => useSaveStatus());
    expect(result.current.state).toBe('idle');
    expect(result.current.message).toBe('');
  });

  it('drives saving → saved on success, then auto-clears', async () => {
    const { result } = renderHook(() => useSaveStatus(3000));

    let resolveFn: () => void = () => {};
    const pending = new Promise<void>((r) => { resolveFn = r; });

    let ran: Promise<boolean>;
    act(() => { ran = result.current.run(() => pending); });
    // Mid-flight: reports "Saving…".
    expect(result.current.state).toBe('saving');
    expect(result.current.message).toBe('Saving…');

    await act(async () => { resolveFn(); await ran; });
    expect(result.current.state).toBe('saved');
    expect(result.current.message).toBe('Saved');

    // Auto-clears back to idle after the delay.
    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current.state).toBe('idle');
    expect(result.current.message).toBe('');
  });

  it('reports the error reason on failure and resolves false', async () => {
    const { result } = renderHook(() => useSaveStatus());
    let ok = true;
    await act(async () => {
      ok = await result.current.run(() => Promise.reject(new Error('Bad region')));
    });
    expect(ok).toBe(false);
    expect(result.current.state).toBe('error');
    expect(result.current.message).toBe('Save failed: Bad region');
  });

  it('honours custom messages', async () => {
    const { result } = renderHook(() => useSaveStatus());
    await act(async () => {
      await result.current.run(() => Promise.resolve(), { saved: 'Rule saved' });
    });
    expect(result.current.message).toBe('Rule saved');
  });

  it('reset() clears immediately', async () => {
    const { result } = renderHook(() => useSaveStatus());
    await act(async () => { await result.current.run(() => Promise.resolve()); });
    expect(result.current.state).toBe('saved');
    act(() => { result.current.reset(); });
    expect(result.current.state).toBe('idle');
    expect(result.current.message).toBe('');
  });
});
