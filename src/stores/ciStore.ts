/**
 * CI watch state (WS-J), keyed by task_id. Fed by the session event stream
 * (ci_progress ticks + a terminal ci_result). Zustand v5 rule (see memory):
 * components select PRIMITIVES or stable refs, never a fresh object per render.
 */
import { create } from 'zustand';
import type { CIJob } from '@/api/ci';

export interface CIWatch {
  task_id: string;
  branch: string;
  status: string; // queued | in_progress | completed | no_run
  conclusion: string;
  run_id: number | null;
  url: string;
  jobs: CIJob[];
  failing: boolean;
  timed_out: boolean;
  cancelled: boolean;
  terminal: boolean;
}

interface CIStoreState {
  watches: Record<string, CIWatch>;
  upsert: (taskId: string, patch: Partial<CIWatch>) => void;
  applyProgress: (taskId: string, ev: Record<string, unknown>) => void;
  applyResult: (taskId: string, payload: Record<string, unknown>) => void;
}

const EMPTY: CIWatch = {
  task_id: '',
  branch: '',
  status: 'queued',
  conclusion: '',
  run_id: null,
  url: '',
  jobs: [],
  failing: false,
  timed_out: false,
  cancelled: false,
  terminal: false,
};

export const useCIStore = create<CIStoreState>((set) => ({
  watches: {},
  upsert: (taskId, patch) =>
    set((s) => ({
      watches: {
        ...s.watches,
        [taskId]: { ...EMPTY, ...(s.watches[taskId] ?? {}), task_id: taskId, ...patch },
      },
    })),
  applyProgress: (taskId, ev) =>
    set((s) => {
      const prev = s.watches[taskId] ?? { ...EMPTY, task_id: taskId };
      return {
        watches: {
          ...s.watches,
          [taskId]: {
            ...prev,
            status: (ev.status as string) ?? prev.status,
            conclusion: (ev.conclusion as string) ?? prev.conclusion,
            run_id: (ev.run_id as number) ?? prev.run_id,
            url: (ev.url as string) ?? prev.url,
            jobs: Array.isArray(ev.jobs) ? (ev.jobs as CIJob[]) : prev.jobs,
          },
        },
      };
    }),
  applyResult: (taskId, payload) =>
    set((s) => {
      const prev = s.watches[taskId] ?? { ...EMPTY, task_id: taskId };
      return {
        watches: {
          ...s.watches,
          [taskId]: {
            ...prev,
            branch: (payload.branch as string) ?? prev.branch,
            status: (payload.status as string) ?? 'completed',
            conclusion: (payload.conclusion as string) ?? prev.conclusion,
            run_id: (payload.run_id as number) ?? prev.run_id,
            url: (payload.url as string) ?? prev.url,
            failing: Boolean(payload.failing),
            timed_out: Boolean(payload.timed_out),
            cancelled: Boolean(payload.cancelled),
            terminal: true,
          },
        },
      };
    }),
}));
