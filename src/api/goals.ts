import { del, get, post } from './client';

/** A session's goal as GET /api/sessions/{id}/goal returns it. */
export interface SessionGoal {
  goal?: string;
  state?: { active?: boolean };
}

const goalUrl = (sessionId: string) => `/api/sessions/${encodeURIComponent(sessionId)}/goal`;

/** Read the goal persisted on the session row. */
export function getSessionGoal(sessionId: string): Promise<SessionGoal> {
  return get<SessionGoal>(goalUrl(sessionId));
}

/** Arm the completion gate with a goal. Rejects with the server's reason. */
export function setSessionGoal(sessionId: string, goal: string): Promise<unknown> {
  return post(goalUrl(sessionId), { goal });
}

/** Clear the goal. Rejects with the server's reason. */
export function clearSessionGoal(sessionId: string): Promise<unknown> {
  return del(goalUrl(sessionId));
}
