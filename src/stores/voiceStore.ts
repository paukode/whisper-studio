import { create } from 'zustand';
import type { VoiceOption } from '@/types/voice';
import type { TeamProgressEvent, TeamReportData } from '@/types/chat';
import { foldTeamProgressIntoMap } from '@/hooks/chatStream/teamProgress';

/**
 * Voice mode (Nova 2 Sonic) UI state. One conversation at a time, bound to the
 * session it was started in. The socket, microphone and playback live in
 * src/services/voiceController.ts; this store only holds what the UI renders.
 *
 * Components select PRIMITIVES from here (zustand v5: never return a fresh
 * object from a selector).
 */
export type VoiceStatus = 'off' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'ending';

export interface VoiceStep {
  id: string;
  name: string;
  status: 'running' | 'ok' | 'error';
  detail: string;
  /** Full tool input, kept for the committed message's activity trace. */
  input?: Record<string, unknown>;
  /** Full tool output when the server sent it (agent tools). */
  output?: string;
  /** Who ran it: Sonic's own app-control tool, or a step inside the delegated
   *  assistant's run. Only assistant steps belong in the written answer. */
  source: 'sonic' | 'assistant';
  /** The delegated run the step belongs to; several can be in flight, and each
   *  written answer takes only its own steps with it. */
  runId?: string;
}

/** The delegated assistant paused for the user: an approval, a question, or a
 *  folder choice. Resolved by voice (Sonic asks) or by the card's buttons. */
export interface VoicePendingRequest {
  kind: 'approval_request' | 'user_question' | 'workspace_prompt';
  /** The delegated run that asked. */
  runId?: string;
  toolUseId: string;
  action: string;
  category: string;
  summary: string;
  question: string;
  options: string[];
  riskHint: string | null;
  detail: string;
}

export interface VoiceUsage {
  inputSpeech: number;
  inputText: number;
  outputSpeech: number;
  outputText: number;
  totalTokens: number;
}

export interface VoiceState {
  status: VoiceStatus;
  /** Session the conversation writes its transcript into. */
  sessionId: string | null;
  voiceId: string;
  modelId: string | null;
  region: string | null;
  /** The assistant's words for the utterance currently being spoken. */
  liveText: string;
  /** Which utterance liveText belongs to (server-assigned). */
  liveUtteranceId: string | null;
  /** Live agent reports of delegated runs, keyed by team id, and which run
   *  each team belongs to. Rendered with the chat's TeamReportCard. */
  teamReports: Record<string, TeamReportData>;
  teamRuns: Record<string, string>;
  /** Sockets still delivering background runs after the user hung up. */
  draining: number;
  /** The chat session the run work on screen belongs to; other sessions
   *  must not show it. */
  runSessionId: string | null;
  /** Tool activity for the utterance in progress (ask_assistant + Claude's steps). */
  steps: VoiceStep[];
  pendingTools: number;
  pendingRequest: VoicePendingRequest | null;
  startedAt: number | null;
  /** When the current Sonic stream opened; the 8-minute renewal counts from here. */
  streamStartedAt: number | null;
  muted: boolean;
  /** Voice stays on but the composer shows the text box (cross-modal typing). */
  typing: boolean;
  error: string | null;
  usage: VoiceUsage | null;
  /** From GET /api/voice/status; null until fetched. */
  available: boolean | null;
  unavailableReason: string | null;
  voices: VoiceOption[];

  begin: (sessionId: string) => void;
  setStatus: (status: VoiceStatus) => void;
  setReady: (modelId: string, voiceId: string) => void;
  appendLiveText: (text: string, utteranceId?: string) => void;
  clearLive: (utteranceId?: string) => void;
  foldTeamEvent: (runId: string, ev: TeamProgressEvent) => void;
  /** Take (and drop) the team reports of one run for its committed message. */
  takeTeamReports: (runId: string) => Record<string, TeamReportData> | undefined;
  adjustDraining: (delta: number) => void;
  clearSteps: () => void;
  /** Drop the steps of one delegated run once its answer is on screen. */
  removeSteps: (runId: string) => void;
  /** Drop everything a run left behind (steps, agent reports, its card). */
  removeRun: (runId: string) => void;
  /** Drop one of Sonic's own steps by tool_use_id. */
  dropStep: (id: string) => void;
  setPendingRequest: (req: VoicePendingRequest | null) => void;
  upsertStep: (step: VoiceStep) => void;
  finishStep: (name: string, status: 'ok' | 'error', detail: string, runId?: string, output?: string) => void;
  adjustPending: (delta: number) => void;
  markStreamStarted: () => void;
  setMuted: (muted: boolean) => void;
  setTyping: (typing: boolean) => void;
  setError: (error: string | null) => void;
  setUsage: (usage: VoiceUsage) => void;
  setAvailability: (info: {
    available: boolean;
    reason: string | null;
    voiceId?: string;
    region?: string;
    voices?: VoiceOption[];
  }) => void;
  setVoiceId: (voiceId: string) => void;
  reset: () => void;
}

const INITIAL = {
  status: 'off' as VoiceStatus,
  sessionId: null as string | null,
  liveText: '',
  liveUtteranceId: null as string | null,
  teamReports: {} as Record<string, TeamReportData>,
  teamRuns: {} as Record<string, string>,
  draining: 0,
  runSessionId: null as string | null,
  steps: [] as VoiceStep[],
  pendingTools: 0,
  pendingRequest: null as VoicePendingRequest | null,
  startedAt: null as number | null,
  streamStartedAt: null as number | null,
  muted: false,
  typing: false,
  error: null as string | null,
  usage: null as VoiceUsage | null,
};

export const useVoiceStore = create<VoiceState>((set, get) => ({
  ...INITIAL,
  voiceId: 'tiffany',
  modelId: null,
  region: null,
  available: null,
  unavailableReason: null,
  voices: [],

  // A new conversation keeps what still belongs to background runs (their
  // steps, agent reports, an unanswered request) so nothing on screen vanishes.
  begin: (sessionId) =>
    set((s) => ({
      ...INITIAL,
      ...keepRunState(s),
      sessionId,
      // Run work of a conversation belongs to the session it started in.
      runSessionId: s.draining > 0 && s.runSessionId ? s.runSessionId : sessionId,
      status: 'connecting',
      startedAt: Date.now(),
    })),
  setStatus: (status) => set({ status }),
  setReady: (modelId, voiceId) =>
    set({ modelId, voiceId, status: 'listening', streamStartedAt: Date.now(), error: null }),
  appendLiveText: (text, utteranceId) =>
    set((s) => {
      const chunk = text.trim();
      if (!chunk) return {};
      // A new utterance replaces the previous one's leftover preview.
      const fresh = utteranceId && s.liveUtteranceId && utteranceId !== s.liveUtteranceId;
      return {
        liveText: s.liveText && !fresh ? `${s.liveText} ${chunk}` : chunk,
        liveUtteranceId: utteranceId ?? s.liveUtteranceId,
      };
    }),
  // Sonic's utterance is done: drop its own steps, keep the delegated run's.
  // With an id, only that utterance's preview is cleared (the next one may
  // already be on screen).
  clearLive: (utteranceId) =>
    set((s) => {
      const keepText = !!utteranceId && !!s.liveUtteranceId && utteranceId !== s.liveUtteranceId;
      return {
        liveText: keepText ? s.liveText : '',
        liveUtteranceId: keepText ? s.liveUtteranceId : null,
        steps: s.steps.filter((st) => st.source === 'assistant'),
      };
    }),
  foldTeamEvent: (runId, ev) =>
    set((s) => {
      const folded = foldTeamProgressIntoMap(s.teamReports, ev);
      if (!folded) return {};
      const teamRuns = ev.team_id && !s.teamRuns[ev.team_id] ? { ...s.teamRuns, [ev.team_id]: runId } : s.teamRuns;
      return { teamReports: folded, teamRuns };
    }),
  takeTeamReports: (runId) => {
    const s = get();
    const mine: Record<string, TeamReportData> = {};
    const rest: Record<string, TeamReportData> = {};
    const teamRuns = { ...s.teamRuns };
    for (const [teamId, report] of Object.entries(s.teamReports)) {
      if ((s.teamRuns[teamId] ?? runId) === runId) {
        mine[teamId] = report;
        delete teamRuns[teamId];
      } else rest[teamId] = report;
    }
    if (Object.keys(mine).length === 0) return undefined;
    set({ teamReports: rest, teamRuns });
    return mine;
  },
  adjustDraining: (delta) => set((s) => ({ draining: Math.max(0, s.draining + delta) })),
  clearSteps: () => set({ steps: [] }),
  removeSteps: (runId) =>
    set((s) => ({ steps: s.steps.filter((st) => !stepBelongsTo(st, runId)) })),
  removeRun: (runId) =>
    set((s) => {
      const teamReports = { ...s.teamReports };
      const teamRuns = { ...s.teamRuns };
      for (const [teamId, owner] of Object.entries(s.teamRuns)) {
        if (owner === runId) {
          delete teamReports[teamId];
          delete teamRuns[teamId];
        }
      }
      return {
        steps: s.steps.filter((st) => !stepBelongsTo(st, runId)),
        teamReports,
        teamRuns,
        pendingRequest: s.pendingRequest?.runId === runId ? null : s.pendingRequest,
      };
    }),
  dropStep: (id) => set((s) => ({ steps: s.steps.filter((st) => st.id !== id) })),
  setPendingRequest: (pendingRequest) => set({ pendingRequest }),
  upsertStep: (step) =>
    set((s) => {
      const idx = s.steps.findIndex((x) => x.id === step.id);
      if (idx === -1) return { steps: [...s.steps, step] };
      const next = s.steps.slice();
      next[idx] = { ...next[idx], ...step };
      return { steps: next };
    }),
  finishStep: (name, status, detail, runId, output) =>
    set((s) => {
      // Claude's inner steps arrive as running/ok pairs by name: settle the
      // most recent running step with that name in the same run.
      for (let i = s.steps.length - 1; i >= 0; i--) {
        const st = s.steps[i];
        if (st.name === name && st.status === 'running' && (!runId || !st.runId || st.runId === runId)) {
          const next = s.steps.slice();
          next[i] = { ...st, status, detail, ...(output ? { output } : {}) };
          return { steps: next };
        }
      }
      return {
        steps: [
          ...s.steps,
          { id: `step-${s.steps.length + 1}`, name, status, detail, source: 'assistant', runId, ...(output ? { output } : {}) },
        ],
      };
    }),
  adjustPending: (delta) => set((s) => ({ pendingTools: Math.max(0, s.pendingTools + delta) })),
  markStreamStarted: () => set({ streamStartedAt: Date.now() }),
  setMuted: (muted) => set({ muted }),
  setTyping: (typing) => set({ typing }),
  setError: (error) => set({ error }),
  setUsage: (usage) => set({ usage }),
  setAvailability: ({ available, reason, voiceId, region, voices }) =>
    set({
      available,
      unavailableReason: reason,
      ...(voiceId ? { voiceId } : {}),
      ...(region ? { region } : {}),
      ...(voices ? { voices } : {}),
    }),
  setVoiceId: (voiceId) => set({ voiceId }),
  // Ending the conversation keeps background-run state while any socket is
  // still draining it.
  reset: () =>
    set((s) => ({
      ...INITIAL,
      ...(s.draining > 0 ? keepRunState(s) : {}),
      voiceId: s.voiceId,
    })),
}));

/** What belongs to delegated runs rather than to one voice conversation. */
function keepRunState(s: VoiceState) {
  return {
    steps: s.steps.filter((st) => st.source === 'assistant'),
    teamReports: s.teamReports,
    teamRuns: s.teamRuns,
    pendingRequest: s.pendingRequest,
    draining: s.draining,
    runSessionId: s.runSessionId,
  };
}

/** A delegated run's step: tagged with its run id, or an untagged assistant
 *  step (older servers), which the first answer to land takes along. */
export const stepBelongsTo = (step: VoiceStep, runId: string): boolean =>
  step.source === 'assistant' && (step.runId === runId || !step.runId);

/** True while a voice conversation is open (any state but off). */
export const isVoiceOn = (status: VoiceStatus): boolean => status !== 'off';
