import React from 'react';

/** The outline glyphs the task list, the agent rows and the agent pane share.
 *  Each is a 14px stroke icon that takes the text colour of its parent. */

const base = {
  width: 14,
  height: 14,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

export const ChevronIcon: React.FC<{ direction: 'right' | 'down' | 'up' }> = ({ direction }) => {
  const points = direction === 'right' ? '9 6 15 12 9 18' : direction === 'down' ? '6 9 12 15 18 9' : '6 15 12 9 18 15';
  return (
    <svg {...base}>
      <polyline points={points} />
    </svg>
  );
};

export const CheckIcon: React.FC = () => (
  <svg {...base}>
    <polyline points="5 12 10 17 19 8" />
  </svg>
);

export const CloseIcon: React.FC = () => (
  <svg {...base}>
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

export const RobotIcon: React.FC = () => (
  <svg {...base}>
    <rect x="4" y="8" width="16" height="12" rx="2" />
    <line x1="12" y1="4" x2="12" y2="8" />
    <circle cx="9" cy="14" r="1" />
    <circle cx="15" cy="14" r="1" />
    <line x1="2" y1="14" x2="4" y2="14" />
    <line x1="20" y1="14" x2="22" y2="14" />
  </svg>
);

/** A spinning arc for a run still working. */
export const LoaderIcon: React.FC = () => (
  <svg {...base} className="icon-spin">
    <path d="M12 3a9 9 0 1 0 9 9" />
  </svg>
);
