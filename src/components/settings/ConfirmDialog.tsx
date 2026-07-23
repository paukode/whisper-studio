import React from 'react';

/** Minimal styled confirmation modal — a nicer, on-brand alternative to
 *  window.confirm for consequential actions (e.g. trusting a skill). */
export const ConfirmDialog: React.FC<{
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}> = ({ title, message, confirmLabel = 'Confirm', danger, onConfirm, onCancel }) => (
  <div
    role="dialog"
    aria-modal="true"
    onClick={onCancel}
    style={{
      position: 'fixed',
      inset: 0,
      background: 'rgba(0,0,0,0.45)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      zIndex: 1100,
    }}
  >
    <div
      onClick={(e) => e.stopPropagation()}
      style={{
        background: 'var(--surface-1, #1e1e1e)',
        color: 'var(--text-primary, #eee)',
        borderRadius: '12px',
        width: 'min(460px, 92vw)',
        padding: '18px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      <strong>{title}</strong>
      <div style={{ fontSize: '14px', lineHeight: 1.5, opacity: 0.9 }}>{message}</div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
        <button className="btn btn-sm" onClick={onCancel} type="button">Cancel</button>
        <button
          className="btn btn-sm"
          onClick={onConfirm}
          type="button"
          style={danger ? { color: 'var(--accent-record)' } : undefined}
        >
          {confirmLabel}
        </button>
      </div>
    </div>
  </div>
);
