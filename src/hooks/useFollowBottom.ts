import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from 'react';

/** Scrolling DOWN to within this distance of the bottom follows again: a
 *  reader who lands near the end does not have to hunt for the last pixel. */
const REJOIN_SLACK_PX = 80;
/** Rounding slack for "exactly at the bottom": a zoomed page reports
 *  fractional scroll offsets, so the pinned bottom can read a pixel short. */
const AT_BOTTOM_PX = 2;

export interface FollowBottom {
  /** Whether the view follows the bottom right now. */
  following: () => boolean;
  /** Scroll to the bottom and follow again (the "N new" pill). */
  jumpToBottom: () => void;
}

/** Where the reader is: the first child still visible at the top edge, and
 *  its distance from that edge. */
interface Anchor {
  node: Element;
  offset: number;
}

const distanceToBottom = (el: HTMLElement): number => el.scrollHeight - el.scrollTop - el.clientHeight;

/** The first child whose bottom edge is below the container's top edge.
 *  Children stack top to bottom, so a binary search finds it. */
function findAnchor(el: HTMLElement): Anchor | null {
  const kids = el.children;
  const top = el.getBoundingClientRect().top;
  let lo = 0;
  let hi = kids.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (kids[mid].getBoundingClientRect().bottom > top) {
      found = mid;
      hi = mid - 1;
    } else {
      lo = mid + 1;
    }
  }
  if (found < 0) return null;
  const node = kids[found];
  return { node, offset: node.getBoundingClientRect().top - top };
}

/**
 * Keep a scroll container on its last line while the reader follows it, and
 * hold it still while they read further up: the transcript's rule. Only the
 * reader's own scrolling switches between the two. Scrolling up stops
 * following; scrolling back down to the bottom follows again.
 *
 * Following is a size rule, not an event rule. Every direct child is under a
 * ResizeObserver, so anything that makes the column taller or shorter (a
 * streamed token, a card that opens or closes, a row that gets its result, a
 * hidden message that renders again) pins the bottom in the same frame,
 * before paint.
 *
 * Reading further up, the child under the top edge keeps its place: when a
 * child above it changes height, the scroll offset moves by the same amount.
 * Chromium calls this scroll anchoring; WebKit (the Mac app) has none, and
 * the chat column hides off-screen messages behind estimated heights that
 * change as they render. The container needs `overflow-anchor: none`, so
 * Chromium does not anchor a second time.
 *
 * `resetKey` (the session) starts following again whenever it changes.
 * `onRejoin` runs each time following starts again.
 */
export function useFollowBottom(
  ref: RefObject<HTMLElement | null>,
  resetKey: unknown,
  onRejoin: () => void,
): FollowBottom {
  const followRef = useRef(true);
  // The scroll offset the last scroll event or write left behind. A scroll
  // event that finds the same offset is the echo of a write made here.
  const lastTopRef = useRef(0);
  const anchorRef = useRef<Anchor | null>(null);
  const onRejoinRef = useRef(onRejoin);
  useEffect(() => {
    onRejoinRef.current = onRejoin;
  }, [onRejoin]);

  const pin = useCallback((el: HTMLElement) => {
    el.scrollTop = el.scrollHeight;
    lastTopRef.current = el.scrollTop;
  }, []);

  const setFollowing = useCallback((next: boolean) => {
    if (followRef.current === next) return;
    followRef.current = next;
    if (next) onRejoinRef.current();
  }, []);

  // Move the scroll offset by however far the anchor moved, then take the
  // anchor again from the corrected position.
  const holdAnchor = useCallback((el: HTMLElement) => {
    const anchor = anchorRef.current;
    if (anchor && anchor.node.parentElement === el) {
      const moved = anchor.node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
      if (Math.abs(moved) >= 0.5) {
        el.scrollTop += moved;
        lastTopRef.current = el.scrollTop;
      }
    }
    anchorRef.current = findAnchor(el);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // A hidden panel (display: none) has no layout: nothing to pin or hold.
    const settle = () => {
      if (el.clientHeight === 0) return;
      if (followRef.current) pin(el);
      else holdAnchor(el);
    };
    const resize = new ResizeObserver(settle);
    resize.observe(el);
    for (const child of Array.from(el.children)) resize.observe(child);
    // A new child joins the observer, whose first report pins or holds. A
    // removed child reports nothing: following, the bottom simply comes up
    // to meet the view; reading, the children below a removed one moved.
    const children = new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node instanceof Element) resize.observe(node);
        });
        record.removedNodes.forEach((node) => {
          if (node instanceof Element) resize.unobserve(node);
        });
      }
      if (!followRef.current && el.clientHeight > 0) holdAnchor(el);
    });
    children.observe(el, { childList: true });

    const onScroll = () => {
      if (el.clientHeight === 0) return;
      const top = el.scrollTop;
      const moved = top - lastTopRef.current;
      lastTopRef.current = top;
      // The echo of a pin or an anchor correction made here.
      if (Math.abs(moved) < 1) return;
      const distance = distanceToBottom(el);
      if (distance <= AT_BOTTOM_PX || (moved > 0 && distance <= REJOIN_SLACK_PX)) {
        setFollowing(true);
      } else if (moved < 0) {
        setFollowing(false);
      }
      if (!followRef.current) anchorRef.current = findAnchor(el);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    pin(el);
    return () => {
      el.removeEventListener('scroll', onScroll);
      children.disconnect();
      resize.disconnect();
    };
  }, [ref, pin, holdAnchor, setFollowing]);

  // Another session opens at its last message, following.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setFollowing(true);
    pin(el);
  }, [ref, resetKey, pin, setFollowing]);

  const following = useCallback(() => followRef.current, []);

  const jumpToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    setFollowing(true);
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [ref, setFollowing]);

  return useMemo(() => ({ following, jumpToBottom }), [following, jumpToBottom]);
}
