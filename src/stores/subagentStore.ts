import { create } from 'zustand';

/**
 * Registry of stop handlers for running background `/subagent` streams, keyed
 * by the subagent's team_id. The `/subagent` handler registers an abort
 * callback when it starts streaming and unregisters when the stream ends; the
 * TeamReportCard shows a Stop button only while a handler is registered (so
 * regular `team_create` cards never show one).
 *
 * Every run also records the chat session that started it (`owners`): Stop
 * and ESC in a session stop only that session's runs, and a session with a
 * run in flight counts as busy, so the runtime registry never evicts it while
 * the run still has an answer to deliver.
 */
interface SubagentState {
  stops: Record<string, () => void>;
  owners: Record<string, string | null>;
  register: (teamId: string, sessionId: string | null, stop: () => void) => void;
  unregister: (teamId: string) => void;
}

export const useSubagentStore = create<SubagentState>((set) => ({
  stops: {},
  owners: {},
  register: (teamId, sessionId, stop) =>
    set((s) => ({
      stops: { ...s.stops, [teamId]: stop },
      owners: { ...s.owners, [teamId]: sessionId },
    })),
  unregister: (teamId) =>
    set((s) => {
      if (!(teamId in s.stops)) return s;
      const stops = { ...s.stops };
      const owners = { ...s.owners };
      delete stops[teamId];
      delete owners[teamId];
      return { stops, owners };
    }),
}));

/** The team ids of the runs a session started. A null session (the draft
 *  view) owns only the runs started from it. */
export function subagentsOf(
  s: Pick<SubagentState, 'owners'>,
  sessionId: string | null,
): string[] {
  return Object.keys(s.owners).filter((teamId) => s.owners[teamId] === sessionId);
}

/** Whether the session has a /subagent run in flight. */
export function hasRunningSubagent(
  s: Pick<SubagentState, 'owners'>,
  sessionId: string | null,
): boolean {
  return subagentsOf(s, sessionId).length > 0;
}
