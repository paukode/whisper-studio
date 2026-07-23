/**
 * Contracts of the global shortcut hook that the ESC stream kill switch
 * relies on: keys consumed by overlay dismissers (defaultPrevented) and IME
 * composition keys are never treated as shortcuts, and a disabled binding
 * attaches no listener at all.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import { useKeyboardShortcut } from './useKeyboardShortcut';

describe('useKeyboardShortcut', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fires the handler on a matching key', () => {
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcut('escape', handler));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('ignores a key already consumed by another handler (defaultPrevented)', () => {
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcut('escape', handler));
    // Simulate an overlay dismisser consuming ESC at the document level —
    // exactly what useDismiss and the modal handlers do.
    const consume = (e: KeyboardEvent) => e.preventDefault();
    document.addEventListener('keydown', consume);
    fireEvent.keyDown(document, { key: 'Escape' });
    document.removeEventListener('keydown', consume);
    expect(handler).not.toHaveBeenCalled();
  });

  it('ignores keys during IME composition', () => {
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcut('escape', handler));
    fireEvent.keyDown(document, { key: 'Escape', isComposing: true });
    expect(handler).not.toHaveBeenCalled();
  });

  it('attaches no listener when disabled', () => {
    const addSpy = vi.spyOn(window, 'addEventListener');
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcut('escape', handler, false));
    expect(addSpy.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(0);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(handler).not.toHaveBeenCalled();
  });

  it('still matches modifier combos as before', () => {
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcut('mod+k', handler));
    fireEvent.keyDown(document, { key: 'k', metaKey: true });
    expect(handler).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: 'k' }); // no modifier — no match
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
