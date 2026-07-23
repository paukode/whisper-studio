import React from 'react';
import type { SaveStatusHandle } from '@/hooks/useSaveStatus';

interface SaveStatusProps {
  status: SaveStatusHandle;
  className?: string;
  style?: React.CSSProperties;
}

// Match the toast glyphs so success/failure read the same across the app.
const ICONS: Record<string, string> = {
  saved: '✓', // ✓
  error: '✗', // ✗
};

/**
 * Inline success/failure indicator for a save action, driven by useSaveStatus.
 * Renders nothing while idle so it never reserves space until a save runs.
 */
export const SaveStatus: React.FC<SaveStatusProps> = ({ status, className, style }) => {
  if (status.state === 'idle' || !status.message) return null;
  const icon = ICONS[status.state];
  return (
    <span
      className={`save-status save-status--${status.state}${className ? ` ${className}` : ''}`}
      role="status"
      aria-live="polite"
      style={style}
    >
      {icon ? `${icon} ` : ''}
      {status.message}
    </span>
  );
};
