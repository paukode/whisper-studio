import { create } from 'zustand';

/**
 * Registry of stop handlers for running background `/subagent` streams, keyed
 * by the subagent's team_id. The `/subagent` handler registers an abort
 * callback when it starts streaming and unregisters when the stream ends; the
 * TeamReportCard shows a Stop button only while a handler is registered (so
 * regular `team_create` cards never show one).
 */
interface SubagentState {
  stops: Record<string, () => void>;
  register: (teamId: string, stop: () => void) => void;
  unregister: (teamId: string) => void;
}

export const useSubagentStore = create<SubagentState>((set) => ({
  stops: {},
  register: (teamId, stop) =>
    set((s) => ({ stops: { ...s.stops, [teamId]: stop } })),
  unregister: (teamId) =>
    set((s) => {
      if (!(teamId in s.stops)) return s;
      const next = { ...s.stops };
      delete next[teamId];
      return { stops: next };
    }),
}));
