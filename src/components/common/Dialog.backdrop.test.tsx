import { render, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DialogHost } from './Dialog';
import { dialogConfirm, useUIStore } from '@/stores/uiStore';

// The shared DialogHost keeps a click-outside-to-cancel affordance, but it must
// only fire on a genuine backdrop press — not when a text selection begins
// inside the dialog and the mouse releases over the backdrop.
describe('DialogHost — backdrop dismissal', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useUIStore.setState({ dialogStack: [] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const overlay = () => document.querySelector('.whisper-dialog-overlay') as HTMLElement;
  const content = () => document.querySelector('.whisper-dialog') as HTMLElement;

  it('cancels on a genuine backdrop press (mousedown + click on the overlay)', async () => {
    render(<DialogHost />);
    act(() => {
      void dialogConfirm({ title: 'Delete?', message: 'Are you sure?' });
    });

    act(() => {
      fireEvent.mouseDown(overlay());
      fireEvent.click(overlay());
    });

    // Close begins immediately (exit animation), then resolves after the fade.
    expect(overlay().className).toContain('whisper-dialog-overlay--leaving');
    await act(async () => {
      vi.advanceTimersByTime(210);
    });
    expect(useUIStore.getState().dialogStack).toHaveLength(0);
  });

  it('does NOT cancel when the press starts inside the dialog and releases on the backdrop', () => {
    render(<DialogHost />);
    act(() => {
      void dialogConfirm({ title: 'Delete?', message: 'Are you sure?' });
    });

    act(() => {
      fireEvent.mouseDown(content()); // selection drag starts inside
      fireEvent.click(overlay()); // …and releases over the backdrop
    });

    expect(overlay().className).not.toContain('--leaving');
    expect(useUIStore.getState().dialogStack).toHaveLength(1);
  });
});
