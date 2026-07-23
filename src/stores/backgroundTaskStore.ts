/** Global background-task state: registry rows keyed by task_id.
 *
 * Fed from two directions: task_event SSE frames (sessionRuntimes upserts a
 * minimal row) and full hydrates from GET /api/background-tasks (panel open,
 * app start). `runningCount` is maintained as a stored primitive so selectors
 * stay zustand-v5-safe (never derive fresh objects in a selector).
 */
import { create } from 'zustand';
import type { BackgroundTaskInfo } from '@/types/backgroundTasks';
import type { TaskEventPayload } from '@/types/chat';

interface BackgroundTaskState {
  tasks: Record<string, BackgroundTaskInfo>;
  runningCount: number;
  panelOpen: boolean;
  upsert: (task: BackgroundTaskInfo) => void;
  applyEvent: (sessionId: string, ev: TaskEventPayload) => void;
  hydrate: () => Promise<void>;
  stopTask: (taskId: string) => Promise<boolean>;
  setPanelOpen: (open: boolean) => void;
}

function countRunning(tasks: Record<string, BackgroundTaskInfo>): number {
  let n = 0;
  for (const id in tasks) if (tasks[id].status === 'running') n += 1;
  return n;
}

export const useBackgroundTaskStore = create<BackgroundTaskState>((set, get) => ({
  tasks: {},
  runningCount: 0,
  panelOpen: false,

  upsert: (task) => {
    set((state) => {
      const tasks = { ...state.tasks, [task.task_id]: task };
      return { tasks, runningCount: countRunning(tasks) };
    });
  },

  applyEvent: (sessionId, ev) => {
    set((state) => {
      const existing = state.tasks[ev.task_id];
      const merged: BackgroundTaskInfo = {
        task_id: ev.task_id,
        kind: ev.kind,
        session_id: sessionId,
        title: ev.title || existing?.title || ev.kind,
        command: existing?.command ?? null,
        status: (ev.status as BackgroundTaskInfo['status']) || 'running',
        exit_code: ev.exit_code ?? existing?.exit_code ?? null,
        output_path: existing?.output_path ?? null,
        result_text: ev.result_tail ?? existing?.result_text ?? null,
        meta: existing?.meta,
        created_at: existing?.created_at ?? ev.timestamp,
        updated_at: ev.timestamp,
        finished_at: ev.event_type === 'task_started' ? null : ev.timestamp,
      };
      const tasks = { ...state.tasks, [ev.task_id]: merged };
      return { tasks, runningCount: countRunning(tasks) };
    });
  },

  hydrate: async () => {
    try {
      const r = await fetch('/api/background-tasks?limit=100');
      if (!r.ok) return;
      const data = (await r.json()) as { tasks: BackgroundTaskInfo[] };
      set(() => {
        const tasks: Record<string, BackgroundTaskInfo> = {};
        for (const t of data.tasks) tasks[t.task_id] = t;
        return { tasks, runningCount: countRunning(tasks) };
      });
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
        if (existing) get().upsert({ ...existing, status: 'stopped' });
      }
      return data.stopped;
    } catch {
      return false;
    }
  },

  setPanelOpen: (open) => set({ panelOpen: open }),
}));
