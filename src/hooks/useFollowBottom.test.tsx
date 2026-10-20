/**
 * The chat follows its last line until the reader scrolls up, and then stays
 * exactly where the reader left it.
 *
 * Approving a card used to throw the view up the conversation in the Mac
 * app: the follow logic reacted only to new rows and streamed text, and the
 * closing card let a message above the view swap its estimated height for
 * its real one. WebKit has no scroll anchoring, so everything on screen
 * moved down by the difference (about 2200 px in a long session), and the
 * view stayed there for as long as the approved action ran.
 *
 * jsdom has no layout, so the column here is a small model: each child has
 * a height, children stack from the top, and scrollTop clamps the way a
 * browser clamps it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { ResizeObserverMock } from '@/test/mocks/resizeObserver';
import { useFollowBottom, type FollowBottom } from './useFollowBottom';

const VIEW = 400;
const heights = new Map<string, number>();
const scrollTops = new WeakMap<Element, number>();

const isColumn = (el: Element) => el instanceof HTMLElement && el.dataset.col !== undefined;
const contentHeight = (col: Element) =>
  Array.from(col.children).reduce((sum, c) => sum + (heights.get((c as HTMLElement).dataset.id ?? '') ?? 0), 0);
const maxTop = (col: Element) => Math.max(0, contentHeight(col) - VIEW);

const saved = {
  scrollTop: Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop'),
  scrollHeight: Object.getOwnPropertyDescriptor(Element.prototype, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(Element.prototype, 'clientHeight'),
  rect: Element.prototype.getBoundingClientRect,
};

let observers: ResizeObserverMock[] = [];

beforeEach(() => {
  heights.clear();
  observers = [];
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get(this: Element) {
      const top = scrollTops.get(this) ?? 0;
      return isColumn(this) ? Math.min(top, maxTop(this)) : top;
    },
    set(this: Element, v: number) {
      scrollTops.set(this, isColumn(this) ? Math.min(Math.max(0, v), maxTop(this)) : v);
    },
  });
  Object.defineProperty(Element.prototype, 'scrollHeight', {
    configurable: true,
    get(this: Element) {
      return isColumn(this) ? Math.max(VIEW, contentHeight(this)) : 0;
    },
  });
  Object.defineProperty(Element.prototype, 'clientHeight', {
    configurable: true,
    get(this: Element) {
      return isColumn(this) ? VIEW : 0;
    },
  });
  Element.prototype.getBoundingClientRect = function (this: Element) {
    if (isColumn(this)) return { top: 0, bottom: VIEW, height: VIEW } as DOMRect;
    const col = this.parentElement as Element;
    let top = -col.scrollTop;
    for (const c of Array.from(col.children)) {
      const h = heights.get((c as HTMLElement).dataset.id ?? '') ?? 0;
      if (c === this) return { top, bottom: top + h, height: h } as DOMRect;
      top += h;
    }
    return { top: 0, bottom: 0, height: 0 } as DOMRect;
  };
  globalThis.ResizeObserver = class extends ResizeObserverMock {
    constructor(cb: ResizeObserverCallback) {
      super(cb);
      observers.push(this);
    }
  } as unknown as typeof ResizeObserver;
});

afterEach(() => {
  for (const [key, desc] of Object.entries(saved)) {
    if (key === 'rect') continue;
    if (desc) Object.defineProperty(Element.prototype, key, desc as PropertyDescriptor);
    else delete (Element.prototype as unknown as Record<string, unknown>)[key];
  }
  Element.prototype.getBoundingClientRect = saved.rect;
  globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;
});

function Column({ ids, resetKey = 's1', onRejoin = () => {}, apiRef }: {
  ids: string[];
  resetKey?: string;
  onRejoin?: () => void;
  apiRef: { current: FollowBottom | null };
}) {
  const ref = useRef<HTMLDivElement>(null);
  const follow = useFollowBottom(ref, resetKey, onRejoin);
  useEffect(() => {
    apiRef.current = follow;
  }, [apiRef, follow]);
  return (
    <div ref={ref} data-col="">
      {ids.map((id) => <div key={id} data-id={id} />)}
    </div>
  );
}

/** A frame's layout pass: every observer reports. */
const layout = () => act(() => {
  for (const o of observers) o._trigger([]);
});

/** Let the MutationObserver report a change to the children. */
const flush = () => act(async () => {});

/** The reader drags the view to `top`. */
const scrollTo = (col: HTMLElement, top: number) => act(() => {
  col.scrollTop = top;
  col.dispatchEvent(new Event('scroll'));
});

const setup = (ids: string[], size = 300) => {
  for (const id of ids) heights.set(id, size);
  const api: { current: FollowBottom | null } = { current: null };
  const onRejoin = vi.fn();
  const view = render(<Column ids={ids} apiRef={api} onRejoin={onRejoin} />);
  const col = view.container.querySelector('[data-col]') as HTMLElement;
  return { ...view, col, api, onRejoin };
};

const topOf = (col: HTMLElement, id: string) =>
  (col.querySelector(`[data-id="${id}"]`) as Element).getBoundingClientRect().top;

describe('useFollowBottom', () => {
  it('opens at the bottom', () => {
    const { col } = setup(['a', 'b', 'c', 'd']);
    expect(col.scrollTop).toBe(maxTop(col));
  });

  it('pins the bottom when a row grows, not only when a message arrives', () => {
    const { col } = setup(['a', 'b', 'c', 'd']);
    heights.set('d', 900);
    layout();
    expect(col.scrollTop).toBe(maxTop(col));
  });

  it('stays at the bottom when a card closes and a message above renders at its real height', () => {
    const { col, rerender, api } = setup(['a', 'b', 'c', 'turn', 'card']);
    // The approval card goes; a message above the view swaps its estimate
    // for its real height.
    heights.set('a', 2500);
    rerender(<Column ids={['a', 'b', 'c', 'turn']} apiRef={api} />);
    layout();
    expect(col.scrollTop).toBe(maxTop(col));
  });

  it('stops following when the reader scrolls up, and keeps their text still', async () => {
    const { col, rerender, api } = setup(['a', 'b', 'c', 'd', 'e', 'f']);
    scrollTo(col, 600);
    expect(api.current?.following()).toBe(false);
    const reading = topOf(col, 'c');

    // Above the reader: a message renders at its real height.
    heights.set('a', 2500);
    layout();
    expect(topOf(col, 'c')).toBe(reading);

    // Below the reader: the reply grows and the card closes.
    heights.set('f', 1200);
    rerender(<Column ids={['a', 'b', 'c', 'd', 'f']} apiRef={api} />);
    await flush();
    layout();
    expect(topOf(col, 'c')).toBe(reading);
  });

  it('keeps the place when a message above the reader is removed', async () => {
    const { col, rerender, api } = setup(['a', 'b', 'c', 'd', 'e', 'f']);
    scrollTo(col, 700);
    const reading = topOf(col, 'd');
    rerender(<Column ids={['b', 'c', 'd', 'e', 'f']} apiRef={api} />);
    await flush();
    expect(topOf(col, 'd')).toBe(reading);
  });

  it('follows again when the reader scrolls back down to the bottom', () => {
    const { col, api, onRejoin } = setup(['a', 'b', 'c', 'd', 'e', 'f']);
    scrollTo(col, 600);
    expect(api.current?.following()).toBe(false);
    // Near the bottom is enough on the way down.
    scrollTo(col, maxTop(col) - 50);
    expect(api.current?.following()).toBe(true);
    expect(onRejoin).toHaveBeenCalledTimes(1);
    heights.set('f', 900);
    layout();
    expect(col.scrollTop).toBe(maxTop(col));
  });

  it('a small scroll up near the bottom still stops following', () => {
    const { col, api } = setup(['a', 'b', 'c', 'd']);
    scrollTo(col, maxTop(col) - 30);
    expect(api.current?.following()).toBe(false);
    heights.set('d', 900);
    layout();
    expect(col.scrollTop).toBe(maxTop(col) - 600 - 30);
  });

  it('keeps following when the bottom comes up to meet the view', () => {
    const { col, rerender, api } = setup(['a', 'b', 'c', 'card']);
    // The card closes; the browser clamps the offset and reports a scroll.
    rerender(<Column ids={['a', 'b', 'c']} apiRef={api} />);
    act(() => {
      col.dispatchEvent(new Event('scroll'));
    });
    expect(api.current?.following()).toBe(true);
    heights.set('c', 900);
    layout();
    expect(col.scrollTop).toBe(maxTop(col));
  });

  it('opens another session at its bottom, following', () => {
    const { col, rerender, api } = setup(['a', 'b', 'c', 'd']);
    scrollTo(col, 0);
    expect(api.current?.following()).toBe(false);
    heights.set('x', 300);
    heights.set('y', 900);
    rerender(<Column ids={['x', 'y', 'a', 'b']} resetKey="s2" apiRef={api} />);
    expect(api.current?.following()).toBe(true);
    expect(col.scrollTop).toBe(maxTop(col));
  });
});
