/**
 * Active-goal state for the completion gate (WS-E), keyed by session_id.
 *
 * Fed from two sources: the session load (GET /api/sessions/{id}/goal, or the
 * goal already on the session row) sets the goal text; live SSE goal_eval /
 * goal_cap_reached / stop_hook_block frames update the last verdict + attempt
 * counter so the GoalBanner reflects progress in real time.
 *
 * Zustand v5 rule (see memory): selectors return PRIMITIVES only — never a
 * fresh object/array — so components subscribe to `useGoalStore(s => s.byId[id]?.goal)`
 * and friends, not the whole record.
 */
import { create } from 'zustand';

export interface GoalEntry {
  goal: string;
  active: boolean;
  lastVerdict: string; // "" | "achieved" | "not_achieved" | "blocked"
  lastFeedback: string;
  attempt: number;
  cap: number;
}

const EMPTY: GoalEntry = {
  goal: '',
  active: false,
  lastVerdict: '',
  lastFeedback: '',
  attempt: 0,
  cap: 8,
};

interface GoalState {
  byId: Record<string, GoalEntry>;
  setGoal: (sessionId: string, goal: string, active: boolean) => void;
  clearGoal: (sessionId: string) => void;
  /** Fold a goal_eval SSE frame (verdict + attempt). */
  applyEval: (
    sessionId: string,
    v: { verdict?: string; feedback?: string; attempt?: number; cap?: number },
  ) => void;
  /** A new user turn: zero the live attempt counter. */
  resetAttempts: (sessionId: string) => void;
}

export const useGoalStore = create<GoalState>((set) => ({
  byId: {},
  setGoal: (sessionId, goal, active) =>
    set((s) => ({
      byId: { ...s.byId, [sessionId]: { ...EMPTY, ...s.byId[sessionId], goal, active } },
    })),
  clearGoal: (sessionId) =>
    set((s) => {
      const next = { ...s.byId };
      delete next[sessionId];
      return { byId: next };
    }),
  applyEval: (sessionId, v) =>
    set((s) => {
      const prev = s.byId[sessionId] ?? EMPTY;
      return {
        byId: {
          ...s.byId,
          [sessionId]: {
            ...prev,
            lastVerdict: v.verdict ?? prev.lastVerdict,
            lastFeedback: v.feedback ?? prev.lastFeedback,
            attempt: v.attempt ?? prev.attempt,
            cap: v.cap ?? prev.cap,
            active: v.verdict === 'achieved' ? false : prev.active,
          },
        },
      };
    }),
  resetAttempts: (sessionId) =>
    set((s) => {
      const prev = s.byId[sessionId];
      if (!prev) return s;
      return { byId: { ...s.byId, [sessionId]: { ...prev, attempt: 0 } } };
    }),
}));
