import { create } from 'zustand';

/** One task from the backend's per-session task tracker (server/tasks_tracker.py).
 *  status is 'pending' | 'in_progress' | 'completed'. */
export interface SessionTask {
  id: string;
  subject: string;
  status: string;
}

interface TaskState {
  /** The authoritative task list per session, fed by the `todo_update` SSE
   *  side-effect (live) and GET /api/tasks/{session} (restore). This is the
   *  single source of truth — the conversation shows a compact row that opens
   *  the tasks panel in the dock, so tasks no longer fragment across turns. */
  tasksBySession: Record<string, SessionTask[]>;
  setTasks: (sessionId: string, tasks: SessionTask[]) => void;
}

export const useTaskStore = create<TaskState>((set) => ({
  tasksBySession: {},
  setTasks: (sessionId, tasks) =>
    set((s) => ({ tasksBySession: { ...s.tasksBySession, [sessionId]: tasks } })),
}));

/** Normalize a raw `todo_update` payload or GET /api/tasks response into
 *  SessionTask[]. Accepts either the bare list or `{ tasks: [...] }`. */
export function normalizeTasks(raw: unknown): SessionTask[] {
  const list = Array.isArray(raw)
    ? raw
    : (raw && typeof raw === 'object' ? (raw as { tasks?: unknown }).tasks : undefined);
  if (!Array.isArray(list)) return [];
  return list
    .map((t) => {
      const o = (t ?? {}) as Record<string, unknown>;
      return {
        id: String(o.id ?? o.task_id ?? ''),
        subject: typeof o.subject === 'string' && o.subject ? o.subject : '(task)',
        status: typeof o.status === 'string' && o.status ? o.status : 'pending',
      };
    })
    .filter((t) => t.status !== 'deleted');
}

/** Progress summary for a task list. */
export function taskProgress(tasks: SessionTask[]): { done: number; total: number; active: boolean } {
  const total = tasks.length;
  const done = tasks.filter((t) => t.status === 'completed').length;
  const active = tasks.some((t) => t.status === 'in_progress');
  return { done, total, active };
}
