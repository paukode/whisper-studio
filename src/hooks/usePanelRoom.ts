import { useLayoutEffect, type RefObject } from 'react';

/** Space a panel keeps from the edge that clips it. */
const EDGE_MARGIN = 12;
/** A panel never gets shorter than this, even in a very short window. */
const MIN_ROOM = 160;

/** The top edge of the nearest ancestor that clips `el` (its overflow is not
 *  visible), or of the window when none does. */
function clipTop(el: HTMLElement): number {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node);
    if (overflowY !== 'visible') return Math.max(0, node.getBoundingClientRect().top);
  }
  return 0;
}

/**
 * Caps an open composer panel's height to the room it has, so it stays in
 * view and scrolls.
 *
 * `.cg-panel` opens upward: its bottom edge is pinned above the composer and
 * it grows toward the top. Its CSS cap assumes the composer sits at the
 * bottom of the window, but on the home screen the composer is in the middle,
 * so a long list (the Workspace recents) ran past the top of the chat area,
 * where the app clips it, and could not scroll. The room is the space between
 * the panel's own bottom edge (which does not move with its height) and the
 * top of the box that clips it. Set as an inline max-height on the panel, and
 * measured again when the window resizes.
 */
export function usePanelRoom(panelRef: RefObject<HTMLElement | null>, open: boolean): void {
  useLayoutEffect(() => {
    const el = panelRef.current;
    if (!open || !el) return;
    const measure = () => {
      const bottom = el.getBoundingClientRect().bottom;
      el.style.maxHeight = `${Math.max(MIN_ROOM, Math.floor(bottom - clipTop(el) - EDGE_MARGIN))}px`;
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [panelRef, open]);
}
