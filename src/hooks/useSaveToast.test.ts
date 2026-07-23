import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';

// useSaveToast reads addToast from the UI store via a selector — mock the store.
const { addToastMock } = vi.hoisted(() => ({ addToastMock: vi.fn() }));
vi.mock('@/stores/uiStore', () => ({
  useUIStore: (selector: (s: { addToast: typeof addToastMock }) => unknown) =>
    selector({ addToast: addToastMock }),
}));

import { useSaveToast } from './useSaveToast';

describe('useSaveToast', () => {
  beforeEach(() => addToastMock.mockReset());

  it('fires a success toast and resolves true', async () => {
    const { result } = renderHook(() => useSaveToast());
    const ok = await result.current(() => Promise.resolve(), { success: 'Skill saved' });
    expect(ok).toBe(true);
    expect(addToastMock).toHaveBeenCalledWith({ type: 'success', message: 'Skill saved' });
  });

  it('fires an error toast with the reason and resolves false', async () => {
    const { result } = renderHook(() => useSaveToast());
    const ok = await result.current(
      () => Promise.reject(new Error('server exploded')),
      { error: 'Failed to save skill' },
    );
    expect(ok).toBe(false);
    expect(addToastMock).toHaveBeenCalledWith({
      type: 'error',
      message: 'Failed to save skill: server exploded',
    });
  });

  it('falls back to default messages', async () => {
    const { result } = renderHook(() => useSaveToast());
    await result.current(() => Promise.resolve());
    expect(addToastMock).toHaveBeenCalledWith({ type: 'success', message: 'Saved' });
  });
});
