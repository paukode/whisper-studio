import { useCallback, useState } from 'react';
import { getChatStore, useActiveChatStore } from '@/stores/sessionRuntimes';
import { useUIStore } from '@/stores/uiStore';
import { resumeAutoMode } from '@/api/settings';

/**
 * Notice shown when the auto-mode classifier's rolling denial counter
 * (server.security.permissions) trips: auto mode stops consulting the
 * classifier and falls back to asking for every remaining approval-gated
 * tool call this turn. "Resume auto mode" is the one-click override —
 * mirrors ApprovalBanner's "allow all this session" affordance, scoped to
 * "for the rest of this turn" instead.
 */
export function AutoModeBreakerBanner() {
  const breaker = useActiveChatStore((s) => s.autoModeBreaker);
  const [isResuming, setIsResuming] = useState(false);

  // Bind the breaker's OWNING session store once, same reasoning as
  // ApprovalBanner: the click can happen after the user has switched to a
  // different session than the one that tripped.
  const handleResume = useCallback(async () => {
    if (!breaker) return;
    setIsResuming(true);
    const chat = getChatStore(breaker.sessionId);
    try {
      await resumeAutoMode(breaker.sessionId);
      chat.getState().clearAutoModeBreaker();
      useUIStore.getState().addToast({
        type: 'success',
        message: 'Auto mode resumed for the rest of this turn.',
        duration: 3000,
        key: 'auto-mode-resumed',
      });
    } catch (err) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Failed to resume auto mode: ${err instanceof Error ? err.message : String(err)}`,
        duration: 5000,
        key: 'auto-mode-resume-error',
      });
    } finally {
      setIsResuming(false);
    }
  }, [breaker]);

  if (!breaker) return null;

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div
        className="ws-approval-card"
        style={{ border: '1px solid var(--accent-warn, #b8860b)', borderRadius: 8, padding: '12px 16px' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: '0.85em', color: 'var(--text-muted)' }}>⚠ Auto mode paused</span>
        </div>
        <div style={{ marginBottom: 12, fontSize: '0.85em', lineHeight: 1.4 }}>{breaker.reason}</div>
        <button
          type="button"
          onClick={() => void handleResume()}
          disabled={isResuming}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            border: '1px solid var(--accent-warn, #b8860b)',
            background: 'transparent',
            color: 'var(--accent-warn, #b8860b)',
            fontSize: '0.82em',
            fontWeight: 500,
            cursor: isResuming ? 'wait' : 'pointer',
            opacity: isResuming ? 0.7 : 1,
          }}
        >
          {isResuming ? 'Resuming…' : '↻ Resume auto mode'}
        </button>
      </div>
    </div>
  );
}
