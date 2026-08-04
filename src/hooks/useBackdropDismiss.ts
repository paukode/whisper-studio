import { useCallback, useRef } from 'react';

/**
 * Backdrop-dismiss handlers for a modal overlay that close ONLY on a genuine
 * backdrop click — a press that both STARTS and ENDS on the overlay itself.
 *
 * The subtle bug this closes: a text selection (or a resize/move drag) that
 * begins inside the dialog content and releases over the backdrop fires a
 * `click` whose target is the overlay — the nearest common ancestor of the
 * mousedown and mouseup targets. A naive `onClick={onDismiss}`, and even an
 * `if (e.target === e.currentTarget)` check, treats that as a backdrop click
 * and wrongly dismisses the dialog ("select text, release outside → window
 * disappears"). Tracking the mousedown target plugs the hole: we only dismiss
 * when the press ALSO started on the backdrop.
 *
 * Spread the returned handlers on the overlay element:
 *
 *   const backdrop = useBackdropDismiss(onClose);
 *   <div className="overlay" {...backdrop}>…</div>
 *
 * This is an opt-in convenience for true modal dialogs that WANT a
 * click-outside-to-cancel affordance. Modals that must never dismiss on the
 * backdrop (e.g. the Settings modal and the config.json editor) simply do not
 * attach a backdrop handler at all.
 */
export function useBackdropDismiss(onDismiss: () => void) {
  const startedOnBackdrop = useRef(false);

  const onMouseDown = useCallback((e: React.MouseEvent<HTMLElement>) => {
    // Only a press that lands on the overlay itself counts as "started on the
    // backdrop". A press on any descendant (content, textarea, button) bubbles
    // here with e.target !== e.currentTarget and disqualifies the gesture.
    startedOnBackdrop.current = e.target === e.currentTarget;
  }, []);

  const onClick = useCallback(
    (e: React.MouseEvent<HTMLElement>) => {
      const started = startedOnBackdrop.current;
      startedOnBackdrop.current = false;
      if (started && e.target === e.currentTarget) onDismiss();
    },
    [onDismiss],
  );

  return { onMouseDown, onClick };
}
