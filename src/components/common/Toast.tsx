import { useEffect, useCallback, useRef } from 'react';

export interface ToastProps {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  /** Optional bold header line (notify_user title). */
  title?: string;
  message: string;
  duration?: number;
  count: number;
  leaving?: boolean;
  shownAt?: number;
  action?: { label: string; href: string };
  onClose: (id: string) => void;
}

const TYPE_ICONS: Record<string, string> = {
  success: '\u2713', // ✓
  error: '\u2717',   // ✗
  warning: '\u26A0', // ⚠
  info: '\u2139',    // ℹ
};

export const Toast: React.FC<ToastProps> = ({
  id,
  type,
  title,
  message,
  duration = 4000,
  count,
  leaving,
  shownAt,
  action,
  onClose,
}) => {
  const progressRef = useRef<HTMLDivElement>(null);

  const handleClose = useCallback(() => {
    onClose(id);
  }, [id, onClose]);

  // Auto-dismiss timer
  useEffect(() => {
    if (duration <= 0) return;
    const timer = window.setTimeout(handleClose, duration);
    return () => window.clearTimeout(timer);
  }, [duration, handleClose, shownAt]);

  // Progress bar animation: animate from 100% → 0% over `duration`
  useEffect(() => {
    const el = progressRef.current;
    if (!el || duration <= 0) return;

    // Reset to full width
    el.style.transition = 'none';
    el.style.width = '100%';
    // Force reflow so the reset applies before the transition starts.
    // getBoundingClientRect() flushes pending layout (same effect as reading
    // offsetWidth) while being a call expression — no unused-value to lint.
    el.getBoundingClientRect();
    // Animate to 0%
    el.style.transition = `width ${duration}ms linear`;
    el.style.width = '0%';
  }, [duration, shownAt]);

  const icon = TYPE_ICONS[type] || TYPE_ICONS.info;
  const className = `whisper-toast whisper-toast--${type}${leaving ? ' whisper-toast--leaving' : ''}`;

  return (
    <div className={className} role="alert">
      <span className="whisper-toast__icon">{icon}</span>
      <span className="whisper-toast__body">
        {title && <strong className="whisper-toast__title">{title}</strong>}
        {message}
      </span>
      {action && (
        <a
          className="whisper-toast__action"
          href={action.href}
          target="_blank"
          rel="noopener noreferrer"
        >
          {action.label}
        </a>
      )}
      {count > 1 && <span className="whisper-toast__count">&times;{count}</span>}
      <button
        className="whisper-toast__close"
        onClick={handleClose}
        aria-label="Close notification"
        type="button"
      >
        &times;
      </button>
      {duration > 0 && (
        <div className="whisper-toast__progress" ref={progressRef} />
      )}
    </div>
  );
};
