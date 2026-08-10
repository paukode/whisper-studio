import React, { useLayoutEffect, useRef, useState } from 'react';

interface ExpandableTextProps {
  text: string;
  className?: string;
  /** Lines to clamp to when collapsed. */
  lines?: number;
}

/**
 * Clamps long settings-row description text to a few lines with a
 * See more / See less toggle. The toggle only appears when the text
 * actually overflows the clamp, so short descriptions render unchanged.
 */
export const ExpandableText: React.FC<ExpandableTextProps> = ({ text, className, lines = 2 }) => {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    // Only measure while collapsed (clamp applied) — re-measuring while
    // expanded would always read "no overflow" since the clamp is removed,
    // which would hide the toggle and strand the user with no way back.
    // `expanded` is a dependency so collapsing back re-measures correctly;
    // the effect just no-ops (leaving the last-known value) while expanded.
    if (!el || expanded) return;
    setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [text, lines, expanded]);

  return (
    <>
      <div
        ref={ref}
        className={className}
        style={
          expanded
            ? undefined
            : {
                display: '-webkit-box',
                WebkitLineClamp: lines,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              }
        }
      >
        {text}
      </div>
      {overflows && (
        <button
          type="button"
          className="settings-item-desc-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'See less' : 'See more'}
        </button>
      )}
    </>
  );
};
