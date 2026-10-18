/** Global background-task state: the registry rows the header's task list
 *  shows, keyed by task_id.
 *
 * Fed from two directions: task_event frames on the app's event channel
 * (sessionRuntimes) and full hydrates from GET /api/background-tasks (app
 * start, list open). A row stays while its task runs and for LINGER_MS after
 * it finishes, so the exit code is still there when the user looks up; then
 * it leaves on its own, or earlier when dismissed. `runningCount` is a stored
 * primitive so selectors stay zustand-v5-safe (never derive fresh objects in
 * a selector).
 */
import { create } from 'zustand';
import type { BackgroundTaskInfo, TaskEventPayload } from '@/types/backgroundTasks';

/** How long a finished row stays in the list. */
export const LINGER_MS = 30_000;

interface BackgroundTaskState {
  tasks: Record<string, BackgroundTaskInfo>;
  runningCount: number;
  panelOpen: boolean;
  /** Rows unfolded to show their details. */
  expanded: Record<string, true>;
  upsert: (task: BackgroundTaskInfo) => void;
  applyEvent: (sessionId: string, ev: TaskEventPayload) => void;
  hydrate: () => Promise<void>;
  stopTask: (taskId: string) => Promise<boolean>;
  /** Remove a finished row now instead of when its linger ends. */
  dismiss: (taskId: string) => void;
  toggleExpanded: (taskId: string) => void;
  setPanelOpen: (open: boolean) => void;
}

function countRunning(tasks: Record<string, BackgroundTaskInfo>): number {
  let n = 0;
  for (const id in tasks) if (tasks[id].status === 'running') n += 1;
  return n;
}

/** When a finished row leaves the list (ms epoch), counted from finished_at. */
export function lingerUntil(task: BackgroundTaskInfo): number {
  const finished = task.finished_at ? Date.parse(task.finished_at) : NaN;
  return (Number.isFinite(finished) ? finished : Date.now()) + LINGER_MS;
}

const leaveTimers = new Map<string, ReturnType<typeof setTimeout>>();

function cancelLeave(taskId: string): void {
  const timer = leaveTimers.get(taskId);
  if (timer === undefined) return;
  clearTimeout(timer);
  leaveTimers.delete(taskId);
}

export const useBackgroundTaskStore = create<BackgroundTaskState>((set, get) => {
  /** Drop a finished row when its linger ends. A row running again by then
   *  (a resumed agent keeps its id) stays. */
  const scheduleLeave = (task: BackgroundTaskInfo): void => {
    cancelLeave(task.task_id);
    if (task.status === 'running') return;
    const delay = Math.max(0, lingerUntil(task) - Date.now());
    leaveTimers.set(
      task.task_id,
      setTimeout(() => {
        leaveTimers.delete(task.task_id);
        if (get().tasks[task.task_id]?.status !== 'running') get().dismiss(task.task_id);
      }, delay),
    );
  };

  const put = (task: BackgroundTaskInfo): void => {
    set((state) => {
      const tasks = { ...state.tasks, [task.task_id]: task };
      return { tasks, runningCount: countRunning(tasks) };
    });
    scheduleLeave(task);
  };

  return {
    tasks: {},
    runningCount: 0,
    panelOpen: false,
    expanded: {},

    upsert: put,

    applyEvent: (sessionId, ev) => {
      const existing = get().tasks[ev.task_id];
      const started = ev.event_type === 'task_started';
      put({
        task_id: ev.task_id,
        kind: ev.kind,
        session_id: sessionId || existing?.session_id || '',
        title: ev.title || existing?.title || ev.kind,
        command: existing?.command ?? null,
        status: (ev.status as BackgroundTaskInfo['status']) || 'running',
        exit_code: ev.exit_code ?? existing?.exit_code ?? null,
        pid: existing?.pid ?? null,
        output_path: existing?.output_path ?? null,
        result_text: ev.result_tail ?? existing?.result_text ?? null,
        meta: existing?.meta,
        created_at: started ? ev.timestamp : (existing?.created_at ?? ev.timestamp),
        updated_at: ev.timestamp,
        finished_at: started ? null : ev.timestamp,
      });
    },

    hydrate: async () => {
      try {
        const r = await fetch('/api/background-tasks?limit=100');
        if (!r.ok) return;
        const data = (await r.json()) as { tasks: BackgroundTaskInfo[] };
        const now = Date.now();
        const tasks: Record<string, BackgroundTaskInfo> = {};
        for (const t of data.tasks) {
          if (t.status === 'running' || lingerUntil(t) > now) tasks[t.task_id] = t;
        }
        for (const id of leaveTimers.keys()) cancelLeave(id);
        set({ tasks, runningCount: countRunning(tasks) });
        for (const t of Object.values(tasks)) scheduleLeave(t);
      } catch {
        // Hydrate is best-effort; live events keep the store usable.
      }
    },

    stopTask: async (taskId) => {
      try {
        const r = await fetch(`/api/background-tasks/${encodeURIComponent(taskId)}/stop`, {
          method: 'POST',
        });
        if (!r.ok) return false;
        const data = (await r.json()) as { stopped: boolean };
        if (data.stopped) {
          // Optimistic: the waiter's task_stopped event confirms shortly.
          const existing = get().tasks[taskId];
          if (existing) put({ ...existing, status: 'stopped' });
        }
        return data.stopped;
      } catch {
        return false;
      }
    },

    dismiss: (taskId) => {
      cancelLeave(taskId);
      set((state) => {
        if (!(taskId in state.tasks)) return state;
        const tasks = { ...state.tasks };
        delete tasks[taskId];
        const expanded = { ...state.expanded };
        delete expanded[taskId];
        return { tasks, expanded, runningCount: countRunning(tasks) };
      });
    },

    toggleExpanded: (taskId) =>
      set((state) => {
        const expanded = { ...state.expanded };
        if (expanded[taskId]) delete expanded[taskId];
        else expanded[taskId] = true;
        return { expanded };
      }),

    setPanelOpen: (open) => set({ panelOpen: open }),
  };
});
