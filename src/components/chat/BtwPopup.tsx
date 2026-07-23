import React, { useCallback } from 'react';

export interface BtwPopupProps {
  question: string;
  answer: string;
  onClose: () => void;
}

/**
 * Floating popup card for /btw side-question answers.
 * Shows the question and answer with an X button to dismiss.
 */
export const BtwPopup: React.FC<BtwPopupProps> = ({ question, answer, onClose }) => {
  const handleOverlayClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  }, [onClose]);

  return (
    <div
      className="btw-popup-overlay"
      onClick={handleOverlayClick}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 2000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(0,0,0,0.3)',
      }}
    >
      <div
        className="btw-popup-card"
        style={{
          background: 'var(--bg-primary)',
          border: '1px solid var(--border)',
          borderRadius: 12,
          padding: '20px 24px',
          maxWidth: 520,
          width: '90%',
          maxHeight: '70vh',
          overflow: 'auto',
          boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
          position: 'relative',
        }}
      >
        <button
          onClick={onClose}
          type="button"
          aria-label="Close"
          style={{
            position: 'absolute',
            top: 12,
            right: 12,
            background: 'none',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            fontSize: 18,
            lineHeight: 1,
            padding: '2px 6px',
            borderRadius: 4,
          }}
        >
          ×
        </button>
        <div style={{ fontSize: '0.8em', color: 'var(--text-muted)', marginBottom: 8 }}>
          /btw {question}
        </div>
        <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
          {answer}
        </div>
      </div>
    </div>
  );
};
