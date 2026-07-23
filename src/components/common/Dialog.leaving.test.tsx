import { render, screen, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { DialogHost } from './Dialog';
import { dialogConfirm, useUIStore } from '@/stores/uiStore';

// The overlay's exit animation (whisper-dialog-overlay--leaving in dialog.css)
// must actually be applied on close, and the dialog must stay mounted until the
// fade finishes before its promise resolves and it leaves the stack.
describe('Dialog exit animation', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useUIStore.setState({ dialogStack: [] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('applies the leaving class, then resolves and unmounts after the fade', async () => {
    render(<DialogHost />);

    let resolved: boolean | null | undefined;
    let promise!: Promise<boolean | null>;
    act(() => {
      promise = dialogConfirm({ title: 'Delete?', message: 'Are you sure?' });
    });
    void promise.then((v) => {
      resolved = v;
    });

    const overlay = () => document.querySelector('.whisper-dialog-overlay');
    expect(overlay()).toBeTruthy();
    expect(overlay()!.className).not.toContain('--leaving');

    // Cancel starts the exit animation but must NOT resolve/unmount yet.
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    });
    expect(overlay()!.className).toContain('whisper-dialog-overlay--leaving');
    expect(useUIStore.getState().dialogStack).toHaveLength(1);
    expect(resolved).toBeUndefined();

    // After the fade window, the promise resolves and the dialog leaves.
    await act(async () => {
      vi.advanceTimersByTime(210);
    });
    await promise;
    expect(resolved).toBeNull();
    expect(useUIStore.getState().dialogStack).toHaveLength(0);
  });

  it('ignores a second cancel while already leaving (no double close)', async () => {
    render(<DialogHost />);
    act(() => {
      void dialogConfirm({ title: 'Delete?', message: 'Are you sure?' });
    });

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    act(() => {
      fireEvent.click(cancel);
      fireEvent.click(cancel); // second click must be a no-op
    });

    // Still exactly one pending timer's worth of state; advancing once clears it.
    await act(async () => {
      vi.advanceTimersByTime(210);
    });
    expect(useUIStore.getState().dialogStack).toHaveLength(0);
  });
});
