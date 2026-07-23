import { useEffect, useRef, type RefObject } from 'react';

/**
 * Dismiss-on-Escape and dismiss-on-outside-click for popups, menus and
 * dialogs — the behaviour ~20 components hand-rolled with slightly
 * different (and occasionally buggy) `document.addEventListener` blocks.
 *
 * The callback is held in a ref so the listeners bind once per
 * enabled/option change instead of re-subscribing on every render.
 *
 * @param ref         element the popup lives in; a pointerdown outside it dismisses.
 * @param onDismiss   called on Escape or outside click.
 * @param opts.enabled       master switch (default true).
 * @param opts.escape        listen for Escape (default true).
 * @param opts.outsideClick  listen for outside pointerdown (default true).
 */
export function useDismiss(
  ref: RefObject<HTMLElement | null>,
  onDismiss: () => void,
  opts: { enabled?: boolean; escape?: boolean; outsideClick?: boolean } = {},
): void {
  const { enabled = true, escape = true, outsideClick = true } = opts;
  const cb = useRef(onDismiss);
  // Sync the latest callback into the ref after render (never during it). The
  // listeners read cb.current only when an event fires, which is always after
  // this effect has committed, so behaviour is unchanged.
  useEffect(() => {
    cb.current = onDismiss;
  });

  useEffect(() => {
    if (!enabled) return;

    const onKey = (e: KeyboardEvent) => {
      if (escape && e.key === 'Escape') {
        // Consume the key: an ESC that dismissed a popup must not also
        // trigger global shortcuts (e.g. the stream kill switch).
        e.preventDefault();
        cb.current();
      }
    };
    const onDown = (e: MouseEvent) => {
      const el = ref.current;
      if (el && !el.contains(e.target as Node)) cb.current();
    };

    if (escape) document.addEventListener('keydown', onKey);
    if (outsideClick) document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [ref, enabled, escape, outsideClick]);
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/**
 * Trap Tab focus within `ref` while `enabled`, and restore focus to the
 * previously-focused element when it unmounts/disables. Modals previously
 * let Tab walk out into the page behind them and never returned focus to
 * the trigger — the two biggest keyboard-a11y gaps.
 *
 * @param opts.initialFocus  focus the first focusable on enable (default true).
 *                           Pass false when the host already manages initial
 *                           focus (e.g. the Dialog renderer focuses per wizard step).
 */
export function useFocusTrap(
  ref: RefObject<HTMLElement | null>,
  enabled = true,
  opts: { initialFocus?: boolean } = {},
): void {
  const { initialFocus = true } = opts;
  useEffect(() => {
    if (!enabled) return;
    const el = ref.current;
    if (!el) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    // No offsetParent/visibility filter: .focus() is a no-op on display:none
    // elements in real browsers anyway, and offsetParent is unimplemented in
    // jsdom (would exclude everything under test). The selector already drops
    // [disabled] and tabindex=-1, which covers the cases that matter here.
    const focusables = () =>
      Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));

    if (initialFocus) {
      const first = focusables()[0];
      (first ?? el).focus?.();
    }

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    el.addEventListener('keydown', onKey);
    return () => {
      el.removeEventListener('keydown', onKey);
      // Restore focus to whatever had it before the trap opened.
      previouslyFocused?.focus?.();
    };
  }, [ref, enabled, initialFocus]);
}
