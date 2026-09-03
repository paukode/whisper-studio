import { createStore } from 'zustand/vanilla';
import type { ChatMessage, ApprovalCategory, ToolUseEvent, PreviewKind, RiskHint, TeamProgressEvent, TeamReportData } from '@/types/chat';
import { foldTeamProgressIntoMap, foldTeamResultsInto } from '@/hooks/chatStream/teamProgress';

/**
 * Generic pending approval. The shape is the same regardless of action —
 * `preview` tells the banner which renderer to use, `payload` carries
 * preview-specific fields (e.g. {path, original, content} for diff,
 * {command} for command, {paths} for list).
 */
export interface PendingApproval {
  toolUseId: string;
  action: string;
  category: string;
  preview: PreviewKind;
  summary: string;
  payload: Record<string, unknown>;
  riskHint?: RiskHint | null;
  explanation?: string | Record<string, unknown> | null;
  sessionId: string;
}

/** Session approval/denial memory per category */
export type SessionApprovalMode = 'allow' | 'deny' | 'ask';
export type SessionApprovals = Record<string, SessionApprovalMode>;

/**
 * Auto-mode circuit breaker notification (server.security.permissions).
 * Set once when the backend's rolling denial counter trips for this turn
 * and auto mode falls back to asking for every remaining approval-gated
 * call; cleared when the user resumes auto mode or a genuinely new turn
 * starts. `sessionId` is the owning session — same reasoning as
 * PendingApproval.sessionId, so the resume action always targets the
 * right store even if the user has switched sessions since it fired.
 */
export interface AutoModeBreaker {
  reason: string;
  sessionId: string;
}

export interface ChatState {
  messages: ChatMessage[];
  isStreaming: boolean;
  currentStreamContent: string;
  currentThinkingContent: string;
  /** Tool traces shown in real-time during streaming */
  currentStreamToolUse: ToolUseEvent[];
  /** Turn-local team reports, folded live from team_progress / team_results
   *  SSE events WHILE the assistant message does not exist yet. Rendered by
   *  StreamingMessage; taken (and cleared) at commit time so the report ends
   *  up on the final assistant message and persists with chat_history. */
  liveTeamReports: Record<string, TeamReportData>;

  /** Queue of pending approvals (FIFO) */
  approvalQueue: PendingApproval[];
  /** Currently displayed approval */
  currentApproval: PendingApproval | null;

  /** Session approval memory — per-category allow/deny/ask */
  sessionApprovals: SessionApprovals;
  sessionDenials: Record<string, boolean>;

  /** Auto-mode circuit breaker notification, or null while it hasn't
   *  tripped (the common case). See AutoModeBreaker above. */
  autoModeBreaker: AutoModeBreaker | null;

  /** Thinking timer state */
  thinkingStartTime: number | null;
  thinkingElapsedMs: number;
  inputTokens: number;
  outputTokens: number;
  estimatedCost: number;
  /** Session-cumulative counterparts: every turn's tokens and cost added up,
   *  which is what the composer readout shows. The per-turn fields above reset
   *  at the head of each turn (each message's meta line reads them); these keep
   *  counting, and are rehydrated from /api/costs/session/{id} when a session is
   *  reopened so they cover the whole conversation, not just this page load. */
  sessionInputTokens: number;
  sessionOutputTokens: number;
  sessionCost: number;
  /** Live context-window usage for the turn, from the usage SSE frame's
   *  context_used/context_max (real per-round token counts). Drives the
   *  composer readout's context meter. 0 = not yet reported this session. */
  contextUsed: number;
  contextMax: number;

  /** SSE diagnostics */
  sseEventCount: number;

  /** Last tool info for empty-bubble fallback */
  lastToolName: string | null;
  lastToolOutput: string | null;
  lastToolError: string | null;

  // Actions
  addMessage: (message: ChatMessage) => void;
  setMessages: (messages: ChatMessage[]) => void;
  appendStreamToken: (token: string) => void;
  appendThinkingToken: (token: string) => void;
  addStreamToolUse: (trace: ToolUseEvent) => void;
  updateStreamToolUse: (toolName: string, updates: Partial<ToolUseEvent>) => void;
  /** Fold one live team_progress event into the turn-local report. */
  foldTeamEvent: (ev: TeamProgressEvent) => void;
  /** Fold the final team_results payload into the turn-local report. */
  foldTeamResults: (payload: unknown) => void;
  /** Return the turn-local reports (undefined when empty) and clear them.
   *  Called exactly once per commit site so the report lands on the message
   *  being committed and never leaks into a later turn. */
  takeTeamReports: () => Record<string, TeamReportData> | undefined;
  setStreaming: (streaming: boolean) => void;
  /** Atomically stop streaming and add the final message in one render pass. */
  finishStream: (message?: ChatMessage) => void;
  /** Commit what the turn has streamed SO FAR as its own assistant message and
   *  reset the live accumulators, WITHOUT ending the turn. Used right before a
   *  standalone card (workflow approval, workspace picker, CI status) is
   *  appended mid-turn, so the card lands below the prose that introduced it
   *  and whatever the model says next lands below the card. */
  flushStreamSegment: (message: ChatMessage) => void;
  clearMessages: () => void;

  // Message editing
  /** Delete a single message at index */
  deleteMessage: (index: number) => void;
  /** Delete message at index and all messages after it */
  deleteMessagesFrom: (index: number) => void;

  // Approval actions
  enqueueApproval: (approval: PendingApproval) => void;
  showNextApproval: () => void;
  clearCurrentApproval: () => void;
  setSessionApproval: (category: ApprovalCategory, mode: SessionApprovalMode) => void;
  resetSessionApprovals: () => void;
  /** Look up the session-memory mode for a category. */
  getSessionApproval: (category: string) => SessionApprovalMode;

  // Auto-mode circuit breaker
  setAutoModeBreaker: (breaker: AutoModeBreaker) => void;
  clearAutoModeBreaker: () => void;

  // Thinking/usage
  setThinkingStart: () => void;
  /** Turn phase streamed by the server: 'preparing' | 'searching' |
   *  'connecting' during setup, or a free-text mid-turn phase (compaction,
   *  continuation after an output-limit cut); null once real output
   *  (thinking/text) arrives or the stream ends. */
  streamStatus: string | null;
  setStreamStatus: (status: string | null) => void;
  setThinkingStop: () => void;
  setUsage: (input: number, output: number, cost?: number, contextUsed?: number, contextMax?: number) => void;
  /** Seed the session-cumulative totals from the server's recorded spend
   *  (session hydration). Never lowers a running total, so it can't undo a
   *  turn that streamed while the fetch was in flight. */
  hydrateSessionUsage: (input: number, output: number, cost: number) => void;
  setThinkingElapsed: (ms: number) => void;

  // Tool tracking
  setLastTool: (name: string, output: string, isError: boolean) => void;

  // SSE diagnostics
  incrementSseCount: () => void;
}

const DEFAULT_SESSION_APPROVALS: SessionApprovals = {
  write: 'ask',
  delete: 'ask',
  cli: 'ask',
};

/** Accessor passed into long-lived stream pipelines (sseStream, team
 *  progress): always resolves the SAME session's state, bound at send
 *  time. Never wire this to the active session — that is exactly the
 *  token-bleed bug parallel sessions exist to fix. */
export type StoreGetter = () => ChatState;

/**
 * Per-session chat store factory. Each live session owns one instance,
 * created and tracked by the runtime registry (sessionRuntimes.ts).
 * There is deliberately no module-level singleton export anymore: UI
 * components bind to the active session via useActiveChatStore, and
 * background pipelines bind to their owning session via getChatStore.
 */
export const createChatStore = () => createStore<ChatState>()((set, get) => ({
  messages: [],
  isStreaming: false,
  currentStreamContent: '',
  currentThinkingContent: '',
  streamStatus: null,
  currentStreamToolUse: [],
  liveTeamReports: {},
  approvalQueue: [],
  currentApproval: null,
  sessionApprovals: { ...DEFAULT_SESSION_APPROVALS },
  sessionDenials: {},
  autoModeBreaker: null,
  thinkingStartTime: null,
  thinkingElapsedMs: 0,
  inputTokens: 0,
  outputTokens: 0,
  estimatedCost: 0,
  sessionInputTokens: 0,
  sessionOutputTokens: 0,
  sessionCost: 0,
  contextUsed: 0,
  contextMax: 0,
  sseEventCount: 0,
  lastToolName: null,
  lastToolOutput: null,
  lastToolError: null,

  addMessage: (message: ChatMessage) => {
    set((state) => ({
      messages: [...state.messages, message],
    }));
  },

  setMessages: (messages: ChatMessage[]) => {
    set({ messages });
  },

  appendStreamToken: (token: string) => {
    set((state) => ({
      currentStreamContent: state.currentStreamContent + token,
    }));
  },

  appendThinkingToken: (token: string) => {
    set((state) => ({
      currentThinkingContent: state.currentThinkingContent + token,
    }));
  },

  addStreamToolUse: (trace: ToolUseEvent) => {
    set((state) => ({
      currentStreamToolUse: [...state.currentStreamToolUse, trace],
    }));
  },

  updateStreamToolUse: (toolName: string, updates: Partial<ToolUseEvent>) => {
    // Update only the FIRST still-running trace with this name. Updating
    // every same-named trace made repeated calls (three task_create) all
    // share the last result; since tools complete in order, the first
    // running one is the call this event belongs to.
    set((state) => {
      const idx = state.currentStreamToolUse.findIndex(
        (t) => t.toolName === toolName && t.status === 'running',
      );
      if (idx === -1) return state;
      const next = state.currentStreamToolUse.slice();
      next[idx] = { ...next[idx], ...updates };
      return { currentStreamToolUse: next };
    });
  },

  foldTeamEvent: (ev: TeamProgressEvent) => {
    set((state) => {
      const folded = foldTeamProgressIntoMap(state.liveTeamReports, ev);
      return folded ? { liveTeamReports: folded } : state;
    });
  },

  foldTeamResults: (payload: unknown) => {
    set((state) => ({
      liveTeamReports: foldTeamResultsInto(state.liveTeamReports, payload),
    }));
  },

  takeTeamReports: () => {
    const reports = get().liveTeamReports;
    if (Object.keys(reports).length === 0) return undefined;
    set({ liveTeamReports: {} });
    return reports;
  },

  setStreaming: (streaming: boolean) => {
    if (streaming) {
      set({
        isStreaming: true,
        currentStreamContent: '',
        currentThinkingContent: '',
        streamStatus: null,
        currentStreamToolUse: [],
        // A fresh turn starts with a clean live report — a prior turn that
        // errored before its commit site could take them must not leak here.
        liveTeamReports: {},
        thinkingStartTime: performance.now(),
        thinkingElapsedMs: 0,
        inputTokens: 0,
        outputTokens: 0,
        estimatedCost: 0,
        contextUsed: 0,
        contextMax: 0,
        sseEventCount: 0,
        lastToolName: null,
        lastToolOutput: null,
        lastToolError: null,
      });
    } else {
      set({
        isStreaming: false,
        streamStatus: null,
        currentStreamContent: '',
        currentThinkingContent: '',
        currentStreamToolUse: [],
        thinkingStartTime: null,
      });
    }
  },

  finishStream: (message?: ChatMessage) => {
    set((state) => {
      // Clear the _inFlight marker on any prior question-group message. It's
      // set by useChatStream while collecting parallel ask_user_question
      // events in a single round so the same message can be appended to;
      // once the round is done, the marker must go so the next round (if
      // any) starts a fresh group instead of mutating the prior one.
      const cleaned = state.messages.map((m) =>
        m._inFlight ? { ...m, _inFlight: undefined } : m,
      );
      return {
        isStreaming: false,
        streamStatus: null,
        currentStreamContent: '',
        currentThinkingContent: '',
        currentStreamToolUse: [],
        thinkingStartTime: null,
        messages: message ? [...cleaned, message] : cleaned,
      };
    });
  },

  flushStreamSegment: (message: ChatMessage) => {
    // Same single-update discipline as finishStream: append the segment and
    // clear the live accumulators in one set() so no render frame shows the
    // text twice (once committed, once still in the streaming bubble).
    // `isStreaming` stays true — the turn is still running, this only closes
    // one segment of it. The thinking clock restarts with the new segment so
    // its live timer reads the wait since the card, not since the top of a
    // turn whose earlier thinking is already committed above.
    set((state) => ({
      messages: [...state.messages, message],
      currentStreamContent: '',
      currentThinkingContent: '',
      currentStreamToolUse: [],
      thinkingStartTime: performance.now(),
      thinkingElapsedMs: 0,
    }));
  },

  clearMessages: () => {
    set({
      messages: [],
      currentStreamContent: '',
      currentThinkingContent: '',
      isStreaming: false,
    });
  },

  // ── Message editing ──

  deleteMessage: (index: number) => {
    set((state) => ({
      messages: state.messages.filter((_, i) => i !== index),
    }));
  },

  deleteMessagesFrom: (index: number) => {
    set((state) => ({
      messages: state.messages.slice(0, index),
    }));
  },

  // ── Approval queue ──

  /**
   * Show this approval, or queue it behind the one already on screen.
   *
   * ALWAYS one of the two — never a silent drop. Routing on session memory
   * (allow → execute, deny → refuse) belongs to the caller, which decides
   * before it gets here; this used to re-check `sessionApprovals` and return
   * early for those modes, so a caller that skipped the check (or a category
   * whose mode changed in between) lost the approval with no card, no queue
   * entry and no trace. Showing a card the user did not strictly need is the
   * safe failure; dropping the only prompt to resume a paused turn is not.
   */
  enqueueApproval: (approval: PendingApproval) => {
    const { currentApproval } = get();
    if (!currentApproval) {
      set({ currentApproval: approval });
    } else {
      set((state) => ({
        approvalQueue: [...state.approvalQueue, approval],
      }));
    }
  },

  showNextApproval: () => {
    const { approvalQueue } = get();
    if (approvalQueue.length > 0) {
      const [next, ...rest] = approvalQueue;
      set({ currentApproval: next, approvalQueue: rest });
    } else {
      set({ currentApproval: null });
    }
  },

  clearCurrentApproval: () => {
    set({ currentApproval: null });
  },

  setSessionApproval: (category: ApprovalCategory, mode: SessionApprovalMode) => {
    set((state) => ({
      sessionApprovals: { ...state.sessionApprovals, [category]: mode },
    }));
  },

  resetSessionApprovals: () => {
    set({ sessionApprovals: { ...DEFAULT_SESSION_APPROVALS } });
  },

  getSessionApproval: (category: string) => {
    return get().sessionApprovals[category] ?? 'ask';
  },

  // ── Auto-mode circuit breaker ──

  setAutoModeBreaker: (breaker: AutoModeBreaker) => {
    set({ autoModeBreaker: breaker });
  },

  clearAutoModeBreaker: () => {
    set({ autoModeBreaker: null });
  },

  // ── Thinking/usage ──

  setStreamStatus: (status: string | null) => {
    set({ streamStatus: status });
  },

  setThinkingStart: () => {
    set({ thinkingStartTime: performance.now() });
  },

  setThinkingStop: () => {
    const { thinkingStartTime } = get();
    if (thinkingStartTime) {
      set({ thinkingElapsedMs: performance.now() - thinkingStartTime });
    }
  },

  setUsage: (input: number, output: number, cost?: number, contextUsed?: number, contextMax?: number) => {
    // Usage frames carry running totals for the CURRENT turn, and every stream
    // start resets the per-turn fields (see setStreaming), so the growth since
    // the last frame is what the session totals accrue. Clamped at 0: a turn
    // that ends and restarts (approval resume, retry) begins its own count
    // again, and that must never subtract from the session.
    const prev = get();
    const dIn = Math.max(0, input - prev.inputTokens);
    const dOut = Math.max(0, output - prev.outputTokens);
    const dCost = cost !== undefined ? Math.max(0, cost - prev.estimatedCost) : 0;
    set({
      inputTokens: input,
      outputTokens: output,
      sessionInputTokens: prev.sessionInputTokens + dIn,
      sessionOutputTokens: prev.sessionOutputTokens + dOut,
      sessionCost: prev.sessionCost + dCost,
      ...(cost !== undefined ? { estimatedCost: cost } : {}),
      ...(contextUsed !== undefined ? { contextUsed } : {}),
      ...(contextMax !== undefined ? { contextMax } : {}),
    });
  },

  hydrateSessionUsage: (input: number, output: number, cost: number) => {
    const prev = get();
    set({
      sessionInputTokens: Math.max(prev.sessionInputTokens, input),
      sessionOutputTokens: Math.max(prev.sessionOutputTokens, output),
      sessionCost: Math.max(prev.sessionCost, cost),
    });
  },

  setThinkingElapsed: (ms: number) => {
    set({ thinkingElapsedMs: ms });
  },

  // ── Tool tracking ──

  setLastTool: (name: string, output: string, isError: boolean) => {
    set({
      lastToolName: name,
      lastToolOutput: output,
      ...(isError ? { lastToolError: output } : {}),
    });
  },

  // ── SSE diagnostics ──

  incrementSseCount: () => {
    set((state) => ({ sseEventCount: state.sseEventCount + 1 }));
  },
}));
