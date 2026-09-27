import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type React from 'react';
import { useResizableDialog, type ResizeEdges } from './useResizableDialog';

// jsdom's window is 1024 x 768 and lays nothing out (offsetWidth is 0), so
// the hook works from its own geometry, which is what these read back.
const KEY = 'test_dialog_geometry';
const VW = 1024;
const VH = 768;

type Hook = ReturnType<typeof useResizableDialog>;

/** The dialog's edges on screen: centered, then offset by `translate`. */
function edges(style: React.CSSProperties, height: number) {
  const w = style.width as number;
  const [x, y] = String(style.translate ?? '0px 0px')
    .split(' ')
    .map((v) => parseFloat(v));
  return {
    left: VW / 2 + x - w / 2,
    right: VW / 2 + x + w / 2,
    top: VH / 2 + y - height / 2,
    bottom: VH / 2 + y + height / 2,
  };
}

function drag(start: (e: React.PointerEvent) => void, dx: number, dy: number) {
  const at = { clientX: 500, clientY: 400, preventDefault() {}, stopPropagation() {} };
  act(() => start(at as unknown as React.PointerEvent));
  act(() => {
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: 500 + dx, clientY: 400 + dy }));
  });
  act(() => {
    window.dispatchEvent(new PointerEvent('pointerup'));
  });
}

const resize = (hook: { current: Hook }, dir: ResizeEdges, dx: number, dy = 0) =>
  drag(hook.current.onResizeStart(dir), dx, dy);

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('useResizableDialog', () => {
  it('keeps the opposite edge still, so the dragged edge follows the pointer', () => {
    const { result } = renderHook(() => useResizableDialog(KEY, { defaultW: 480, minW: 320, minH: 260 }));
    const before = edges(result.current.style, 0);

    resize(result, { e: true }, 100);
    const east = edges(result.current.style, 0);
    expect(east.left).toBe(before.left);
    expect(east.right).toBe(before.right + 100);

    resize(result, { w: true }, -60);
    const west = edges(result.current.style, 0);
    expect(west.right).toBe(east.right);
    expect(west.left).toBe(east.left - 60);
  });

  it('pins the height on a vertical drag and keeps the other edge in place', () => {
    const { result } = renderHook(() => useResizableDialog(KEY, { defaultW: 480, minW: 320, minH: 260 }));
    expect(result.current.sized).toBe(false);

    // From natural height (jsdom has none, so the minimum) down by 100.
    resize(result, { s: true }, 0, 100);
    expect(result.current.sized).toBe(true);
    expect(result.current.style.height).toBe(360);
    const south = edges(result.current.style, 360);
    expect(south.top).toBe(VH / 2 - 260 / 2);

    resize(result, { n: true }, 0, -40);
    const north = edges(result.current.style, 400);
    expect(result.current.style.height).toBe(400);
    expect(north.bottom).toBe(south.bottom);
  });

  it('stops at the viewport margin and never goes below the minimum', () => {
    const { result } = renderHook(() =>
      useResizableDialog(KEY, { defaultW: 480, minW: 320, minH: 260, margin: 16 }),
    );
    resize(result, { e: true }, 2000);
    const wide = edges(result.current.style, 0);
    expect(result.current.style.width).toBe(VW - 32);
    expect(wide.left).toBe(16);
    expect(wide.right).toBe(VW - 16);

    resize(result, { w: true }, 5000);
    expect(result.current.style.width).toBe(320);
  });

  it('remembers the geometry, and reset returns to the centered default', () => {
    const first = renderHook(() => useResizableDialog(KEY, { defaultW: 480 }));
    resize(first.result, { e: true }, 120);
    first.unmount();

    const { result } = renderHook(() => useResizableDialog(KEY, { defaultW: 480 }));
    expect(result.current.style.width).toBe(600);
    expect(result.current.style.translate).toBe('60px 0px');

    act(() => result.current.reset());
    expect(result.current.style.width).toBe(480);
    expect(result.current.style.translate).toBeUndefined();
    expect(result.current.style.height).toBeUndefined();
  });
});
