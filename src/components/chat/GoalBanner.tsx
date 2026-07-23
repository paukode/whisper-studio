/**
 * Goal banner above the chat input: shows the active goal, the last gate
 * verdict, the attempt counter, and a clear button. Visible only while a goal
 * is active for the current session. Zustand v5 rule: primitive selectors only.
 */
import React, { useCallback, useEffect } from 'react';
import { useGoalStore } from '@/stores/goalStore';

const VERDICT_LABEL: Record<string, string> = {
  achieved: 'achieved',
  not_achieved: 'working…',
  blocked: 'blocked',
};

const VERDICT_COLOR: Record<string, string> = {
  achieved: 'var(--accent-ok, #2e7d32)',
  not_achieved: 'var(--accent-warn, #b8860b)',
  blocked: 'var(--accent-record)',
};

export const GoalBanner: React.FC<{ sessionId: string | null }> = ({ sessionId }) => {
  const goal = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.goal : '') ?? '');
  const active = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.active : false) ?? false);
  const verdict = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.lastVerdict : '') ?? '');
  const attempt = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.attempt : 0) ?? 0);
  const cap = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.cap : 8) ?? 8);

  const clear = useCallback(() => {
    if (!sessionId) return;
    useGoalStore.getState().clearGoal(sessionId);
    void fetch(`/api/sessions/${sessionId}/goal`, { method: 'DELETE' }).catch(() => {});
  }, [sessionId]);

  // Hydrate from the server when the session changes (the goal persists on the
  // session row across reloads).
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    void fetch(`/api/sessions/${sessionId}/goal`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        if (data.goal && data.state?.active) {
          useGoalStore.getState().setGoal(sessionId, data.goal, true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (!sessionId || !goal || !active) return null;

  return (
    <div className="goal-banner" role="status">
      <span className="goal-banner-icon" aria-hidden="true">🎯</span>
      <span className="goal-banner-text" title={goal}>{goal}</span>
      {verdict && (
        <span className="goal-banner-chip" style={{ color: VERDICT_COLOR[verdict] ?? 'inherit' }}>
          {VERDICT_LABEL[verdict] ?? verdict}
        </span>
      )}
      {attempt > 0 && (
        <span className="goal-banner-attempts" title="Consecutive gate blocks this turn">
          {attempt}/{cap}
        </span>
      )}
      <button type="button" className="goal-banner-clear" onClick={clear} aria-label="Clear goal">
        ✕
      </button>
    </div>
  );
};
