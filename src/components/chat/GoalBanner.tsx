/**
 * Goal banner above the chat input: shows the active goal, the last gate
 * verdict, the attempt counter, and a clear button. Visible only while a goal
 * is active for the current session. Zustand v5 rule: primitive selectors only.
 */
import React, { useCallback, useEffect } from 'react';
import { useGoalStore } from '@/stores/goalStore';
import { useUIStore } from '@/stores/uiStore';
import { clearSessionGoal, getSessionGoal } from '@/api/goals';
import { toError } from '@/utils/toError';
import type { GoalVerdict } from '@/types/chat';
import { ICON } from './activityIcons';

const VERDICT_LABEL: Record<GoalVerdict, string> = {
  achieved: 'achieved',
  not_achieved: 'working…',
  blocked: 'blocked',
  // The judge could not run: neither a pass nor a blocker, so it reads
  // neutral, and the reason is on the chip's tooltip and the goal card.
  not_checked: 'not checked',
};

const VERDICT_COLOR: Record<GoalVerdict, string> = {
  achieved: 'var(--success)',
  not_achieved: 'var(--risk-warn)',
  blocked: 'var(--accent-record)',
  not_checked: 'var(--text-muted)',
};

export const GoalBanner: React.FC<{ sessionId: string | null }> = ({ sessionId }) => {
  const goal = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.goal : '') ?? '');
  const active = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.active : false) ?? false);
  const verdict = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.lastVerdict : '') ?? '');
  const feedback = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.lastFeedback : '') ?? '');
  const attempt = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.attempt : 0) ?? 0);
  const cap = useGoalStore((s) => (sessionId ? s.byId[sessionId]?.cap : 8) ?? 8);

  // The banner goes only once the server has dropped the goal, so it never
  // hides a goal the completion gate still enforces.
  const clear = useCallback(async () => {
    if (!sessionId) return;
    try {
      await clearSessionGoal(sessionId);
      useGoalStore.getState().clearGoal(sessionId);
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: `Could not clear the goal: ${toError(err).message}` });
    }
  }, [sessionId]);

  // Hydrate from the server when the session changes (the goal persists on the
  // session row across reloads).
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    void getSessionGoal(sessionId)
      .then((data) => {
        if (cancelled) return;
        if (data.goal && data.state?.active) {
          useGoalStore.getState().setGoal(sessionId, data.goal, true);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        useUIStore.getState().addToast({
          type: 'error',
          key: 'goal-hydrate',
          message: `Could not read this session's goal: ${toError(err).message}`,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (!sessionId || !goal || !active) return null;

  return (
    <div className="goal-banner" role="status">
      <span className="goal-banner-icon" aria-hidden="true">{ICON.flag}</span>
      <span className="goal-banner-text" title={goal}>{goal}</span>
      {verdict && (
        <span
          className={`goal-banner-chip verdict-${verdict}`}
          style={{ color: VERDICT_COLOR[verdict] ?? 'inherit' }}
          title={feedback || undefined}
        >
          {VERDICT_LABEL[verdict] ?? verdict}
        </span>
      )}
      {attempt > 0 && (
        <span className="goal-banner-attempts" title="Consecutive gate blocks this turn">
          {attempt}/{cap}
        </span>
      )}
      <button type="button" className="goal-banner-clear" onClick={() => void clear()} aria-label="Clear goal">
        {ICON.x}
      </button>
    </div>
  );
};
