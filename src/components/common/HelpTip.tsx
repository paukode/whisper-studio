import React, { useId } from 'react';

interface HelpTipProps {
  /** Tooltip body shown on hover/focus, exposed as the button's description. */
  text: string;
}

/**
 * Small "?" button with a hover/focus tooltip. The app otherwise sticks to
 * native `title` attributes; this shared component exists for the composer
 * popovers, where per-option descriptions moved out of the rows and behind an
 * explicit affordance. A real button so keyboards and touch can reach it
 * (tap focuses, showing the bubble); clicks are swallowed so a tip inside a
 * selectable row never triggers the row. Escape dismisses just the tip
 * (preventDefault so the global ESC stack does not also fire).
 */
export const HelpTip: React.FC<HelpTipProps> = ({ text }) => {
  const id = useId();
  return (
    <button
      type="button"
      className="help-tip"
      aria-label="Help"
      aria-describedby={id}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          e.currentTarget.blur();
        }
      }}
    >
      ?
      <span className="help-tip-bubble" role="tooltip" id={id}>{text}</span>
    </button>
  );
};
