/**
 * useChatStream — SSE chat stream handler.
 *
 * Parallel-sessions contract: a send binds its OWNING session's chat
 * store at send time and never lets go. The stream keeps writing into
 * that session — visible or not — while the user switches around. One
 * in-flight stream per session (a re-send aborts only that session's
 * stream), up to MAX_ACTIVE_SESSIONS sessions active at once.
 *
 * The SSE parsing engine (readSSEStream) and the approval-continuation
 * pump (sendApprovalContinuation) live in ./chatStream/sseStream; the
 * team-report folding helpers live in ./chatStream/teamProgress. This file
 * keeps the React hook that wires them to component state.
 */
import { useCallback, useEffect } from 'react';
import {
  countActiveSessions,
  getChatStore,
  getTranscriptionStore,
} from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { refreshSessionUsage } from '@/stores/sessionUsage';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useIndexSearchStore } from '@/stores/indexSearchStore';
import type { ChatMessage, ToolUseEvent } from '@/types/chat';
import { toError } from '@/utils/toError';
import { ensureRetentionEnabled } from '@/components/chat/dataRetentionConsent';
import { _sseEventLog, readSSEStream } from './chatStream/sseStream';
import { emptyResponseFallback } from './chatStream/emptyResponse';
import { pausedTurnSettings, turnModelSettings } from './chatStream/turnSettings';
import { buildHistoryPayload } from './chatStream/history';
import { APPROVED_ACTION_STATUS } from './chatStream/approvalLeg';
import {
  abortSessionStream,
  buildStoppedMessage,
  hasLiveStream,
  killSessionStream,
  registerStreamController,
  releaseStreamController,
  wasKillFinalized,
} from './chatStream/streamControl';

export interface SendOptions {
  forceSkill?: string;
  /** What the user's chat bubble shows, when it should differ from the
   *  `question` sent to the model — e.g. a bare `@skill` mention whose
   *  payload carries a synthetic anchor sentence the user never typed. */
  displayText?: string;
  hideUserMessage?: boolean;
  /** Either a single tool_result (single ask_user_question / approval) or
   *  an array (multi-question batch submit from a tabbed card). The backend
   *  accepts both shapes via /api/chat. */
  approvedToolResult?:
    | { tool_use_id: string; content: string }
    | Array<{ tool_use_id: string; content: string }>;
  attachmentIds?: string[];
  attachmentNames?: string[];
}

export interface UseChatStreamReturn {
  send: (question: string, opts?: SendOptions) => Promise<void>;
  /** Send a message while a turn for this session is ALREADY streaming.
   *  Delivered into that running turn instead of starting (or being
   *  refused by) a second one — see server/chat/engine/midturn_inbox.py.
   *  Resolves to whether it was actually delivered (strictly binary: no
   *  message is shown, and no state changes, unless this is true). */
  sendMidTurn: (question: string) => Promise<boolean>;
  abort: () => void;
}

/** What the user's bubble shows. For forced-skill sends the mention is
 *  prepended and only the user's own text follows it — `displayText`
 *  (possibly empty) takes precedence over the payload `question`, which may
 *  carry a synthetic anchor sentence that must never be rendered. */
export function buildDisplayQuestion(
  question: string,
  opts?: Pick<SendOptions, 'forceSkill' | 'displayText'>,
): string {
  const shown = opts?.displayText ?? question;
  return opts?.forceSkill ? `@${opts.forceSkill}${shown ? ' ' + shown : ''}` : shown;
}

/** The user-facing parallelism ceiling: how many sessions may be ACTIVE
 *  (streaming / mid-approval / recording) at once. */
export const MAX_ACTIVE_SESSIONS = 3;

/** A JSON answer from /api/chat: the "queued into the running turn" reply
 *  (server/chat/routes.py) or a refusal carrying its reason. */
interface ChatJsonReply {
  queued_into_running_turn?: boolean;
  error?: string;
  detail?: unknown;
}

function isJsonReply(response: Response): boolean {
  return (response.headers.get('content-type') || '').includes('application/json');
}

async function readJsonReply(response: Response): Promise<ChatJsonReply | null> {
  try {
    const data: unknown = await response.json();
    return data && typeof data === 'object' ? (data as ChatJsonReply) : null;
  } catch {
    return null;
  }
}

/** The reason a JSON reply gives, in the server's own words when it has any. */
function jsonReplyError(data: ChatJsonReply | null, status: number): string {
  if (data?.error) return data.error;
  if (typeof data?.detail === 'string' && data.detail) return data.detail;
  return `Chat request failed: unexpected reply (HTTP ${status})`;
}

/** The one notice for "your text went into the turn already running in this
 *  session", shared by the composer's steer path and a full send the server
 *  queued the same way. */
function toastQueuedIntoRunningTurn(): void {
  useUIStore.getState().addToast({
    type: 'info',
    message:
      'Added to the running turn. The assistant will take it into account as it continues and answer it when this turn finishes.',
    duration: 5000,
    key: 'midturn-queued',
    persist: false,
  });
}

/** A send that replaced this session's own live stream got queued into
 *  that stream's turn, which is closing: say so, since no reply will come. */
function toastQueuedIntoReplacedTurn(): void {
  useUIStore.getState().addToast({
    type: 'error',
    message:
      'Not answered: the turn this message replaced was still closing on the server and took the message, so no reply will come. Use Regenerate on your message to send it again.',
    duration: 8000,
    key: 'queued-into-replaced-turn',
    source: 'chat',
  });
}

// Sessions with a mid-turn message still waiting for the server's answer.
// The composer keeps the text until the server confirms it, so a slow
// confirmation looked like a message that went nowhere and invited another
// Enter; every press posted the same text again, and once the server caught
// up it queued each copy into the running turn.
const midTurnInFlight = new Set<string>();

// The controller registry and the instant kill switch live in
// ./chatStream/streamControl (a leaf module shared with sseStream).
export { abortSessionStream, killSessionStream };

/**
 * Hook providing chat stream send/abort functionality. `send` targets the
 * session that is active at CALL time; everything after that is bound.
 */
export function useChatStream(): UseChatStreamReturn {
  // Re-attach the live SSE event log to ``window.__lastSSE`` on each
  // mount so HMR doesn't strand devtools subscribers on a stale array.
  useEffect(() => {
    window.__lastSSE = _sseEventLog;
    return () => {
      // Leave the reference in place on unmount — devtools probes are
      // single-shot reads, not subscriptions, so a stale-but-correct
      // pointer is better than ``undefined`` between mounts.
    };
  }, []);

  const send = useCallback(async (question: string, opts?: SendOptions) => {
    const sessionStore = useSessionStore.getState;
    const settings = useSettingsStore.getState();

    // Gate Mythos-class models (e.g. Fable 5) behind data-retention consent.
    // This covers the case where such a model is the default/selected on load
    // with no explicit picker change. If the user declines, abort the send.
    const selModel = settings.models.find((m) => m.key === settings.selectedModel);
    if (selModel?.requires_data_retention && !settings.dataRetentionEnabled) {
      const ok = await ensureRetentionEnabled();
      if (!ok) return;
    }

    // Resolve the owning session ONCE — every store access below goes
    // through this binding, so a mid-stream session switch changes
    // nothing about where this stream writes.
    let activeSessionId = sessionStore().currentSessionId;
    if (!activeSessionId) {
      activeSessionId = sessionStore().createSession();
    }
    const chat = getChatStore(activeSessionId);
    const store = () => chat.getState();

    // Parallelism ceiling: activating a session beyond the cap gets a
    // warning, not a queue. Re-sends/continuations in an already-active
    // session always pass.
    const alreadyActive = store().isStreaming || store().currentApproval !== null;
    if (!alreadyActive && countActiveSessions(activeSessionId) >= MAX_ACTIVE_SESSIONS) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `${MAX_ACTIVE_SESSIONS} sessions are already active. Wait for one to finish (or stop it) before starting another.`,
        duration: 5000,
        key: 'parallel-cap',
        source: 'chat',
      });
      return;
    }

    // Abort any in-flight stream FOR THIS SESSION only. Remember whether
    // there was one: the server releases that turn's slot only once it sees
    // the disconnect, so for a moment it still counts the turn as running and
    // may queue this send into the very turn just cancelled here. (A leg
    // still running its approved action holds no server slot.)
    const replacesLiveStream =
      hasLiveStream(activeSessionId) && store().streamStatus !== APPROVED_ACTION_STATUS;
    abortSessionStream(activeSessionId);

    const isContinuation = !!opts?.approvedToolResult;

    // Add user message (unless hidden or continuation)
    if (!opts?.hideUserMessage && !isContinuation) {
      const displayQuestion = buildDisplayQuestion(question, opts);
      const userMsg: ChatMessage = {
        role: 'user',
        content: displayQuestion,
        timestamp: new Date().toISOString(),
        ...(opts?.attachmentNames?.length ? { attachmentNames: opts.attachmentNames } : {}),
        ...(opts?.attachmentIds?.length ? { attachmentIds: opts.attachmentIds } : {}),
      };
      store().addMessage(userMsg);
    }

    // A genuinely new turn (not a continuation) starts with a clean auto-mode
    // breaker notice — it's turn-scoped on the backend too (reset_auto_mode_breaker
    // fires on the same is_new_turn signal), so a banner left over from a prior
    // turn must not linger into this one.
    if (!isContinuation) {
      store().clearAutoModeBreaker();
    }

    store().setStreaming(true);

    const controller = new AbortController();
    // An answered question or folder card resumes the turn that asked; only a
    // new message starts a turn (the `since` a Stop scopes its kill to).
    registerStreamController(activeSessionId, controller, { startsTurn: !isContinuation });

    // Build transcript from the OWNING session's transcript store. Keep the
    // speaker label on every line (same format as the panel/sidebar exports)
    // so summary skills can attribute lines and list attendees from diarization.
    const { segments, speakerNames } = getTranscriptionStore(activeSessionId).getState();
    const withSpeakers = (segs: typeof segments) =>
      segs.map(s => `[${speakerNames[s.speaker] ?? s.speaker}] ${s.text}`).join('\n');
    const transcript = opts?.forceSkill === 'catch_up'
      ? withSpeakers(segments.slice(-5))
      : withSpeakers(segments);

    // Build history (exclude just-added user message). UI-only rows
    // (role='cron_event') are filtered here too — the backend filters
    // again via visible_chat_history() as defence-in-depth, but it's
    // wasteful to ship them over the wire.
    const cappedHistory = buildHistoryPayload(store().messages, isContinuation);

    // Model + effort + response length. Resolved once: the body carries them,
    // and any card this turn pauses on keeps the same values so the resumed
    // half can never finish on different settings than it started. An answer
    // to a question or folder prompt IS such a resumed half: it runs on the
    // settings recorded with its card, not on what the window-wide picker
    // shows now (another session may have changed it while the card waited).
    const answered = opts?.approvedToolResult;
    const settingsForTurn = (answered && pausedTurnSettings(
      store().messages,
      (Array.isArray(answered) ? answered : [answered]).map((a) => a.tool_use_id),
    )) || turnModelSettings();
    const body: Record<string, unknown> = {
      question,
      transcript,
      history: cappedHistory,
      attachment_ids: opts?.attachmentIds ?? [],
      attachment_names: opts?.attachmentNames ?? [],
      ...settingsForTurn,
      force_skill: opts?.forceSkill ?? null,
      session_id: activeSessionId,
      // Local thinking and tools have no per-turn client flags any more: the
      // backend keys both on the model registry's capability flags, so capable
      // on-device models always think and always get the full tool pool.
      // The CTX slider value DOES ride on every turn: it is the source of
      // truth for the llama-server context size, so a lazy server start after
      // a backend restart can never run at a stale size (the chip and the
      // server always agree). Ignored by cloud models.
      local_context_window: settings.localContextWindow,
      session_denials: store().sessionDenials,
      session_approvals: store().sessionApprovals,
      approved_tool_result: opts?.approvedToolResult ?? null,
      // Indexes the user selected for this session (point D). Send the stored
      // selection verbatim; when there's no entry yet (a brand-new session whose
      // id was just minted here, so ChatInput's seeding effect hasn't run), send
      // `undefined` — JSON.stringify drops the key, and the backend treats an
      // ABSENT field as "all indexed folders" so the first question still grounds.
      // An explicit empty array (user deselected all) is preserved and means
      // "search nothing".
      selected_search_indexes:
        useIndexSearchStore.getState().selectionBySession[activeSessionId],
    };
    // MCP servers are resolved by the backend from each server's persisted
    // `enabled` flag (toggled live in Settings > MCP), so we don't send a
    // per-request override.

    let fullResponse = '';

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (response.status === 409) {
        // Server-side same-session guard: this session already has a stream in
        // flight (another tab, or a previous reply whose connection was
        // suspended/abandoned). Surface the server's actionable guidance: the
        // slot auto-reclaims after a short timeout, or the user can reset it
        // immediately from the chat ⋯ menu.
        store().finishStream();
        let msg = 'This session is busy. If it looks stuck, reset it from the chat ⋯ menu.';
        try {
          const data = await response.json() as { error?: string };
          if (data?.error) msg = data.error;
        } catch { /* keep the fallback message */ }
        useUIStore.getState().addToast({
          type: 'error',
          message: msg,
          duration: 6000,
          key: 'parallel-409',
          source: 'chat',
        });
        return;
      }
      // A JSON body is never an SSE stream. The server answers a send that
      // lands while a turn is still running in this session by queueing the
      // text into that turn, and every refusal carries its reason as JSON.
      // Handing either to the SSE reader parsed zero events and ended the
      // send in total silence: no reply, no toast, no error.
      if (isJsonReply(response)) {
        const data = await readJsonReply(response);
        if (response.ok && data?.queued_into_running_turn === true) {
          // The user's bubble stays and the thinking indicator ends here.
          store().finishStream();
          if (replacesLiveStream) {
            // The running turn the server queued into is the one this send
            // just aborted: it ends on the disconnect and nothing answers.
            toastQueuedIntoReplacedTurn();
          } else {
            // Delivered into a turn that really runs; say where it went.
            toastQueuedIntoRunningTurn();
          }
          return;
        }
        throw new Error(jsonReplyError(data, response.status));
      }
      if (!response.ok || !response.body) {
        throw new Error(`Chat request failed: ${response.status}`);
      }

      const result = await readSSEStream(response, activeSessionId, controller.signal, settingsForTurn);
      // A kill switch may have finalized this stream while the read loop was
      // draining (signal.aborted exits the loop NORMALLY, landing here on the
      // success path) — appending anything now would resurrect the answer.
      if (wasKillFinalized(controller)) return;
      fullResponse = result.fullResponse;

      // Empty-bubble fallback (matching original chat-stream.js). Shared with
      // the approval-resume leg — see ./chatStream/emptyResponse.
      // Skip when a user_question was emitted — the question card IS the response
      // — and when a mid-turn card already flushed this turn's text into the
      // transcript, where "no text response from the model" would be a lie
      // printed directly under the model's own paragraphs.
      if (
        !fullResponse &&
        !result.hasPendingApprovals &&
        !result.hasUserQuestion &&
        result.flushedSegments === 0
      ) {
        fullResponse = emptyResponseFallback(store());
      }

      // Build tool trace entries from accumulated skill data.
      const finalToolUse: ToolUseEvent[] = result.skillTraces.map(t => ({
        toolId: t.name,
        toolName: t.name,
        input: t.input ?? {},
        result: t.output || undefined,
        status: 'complete' as const,
        previewImage: t.previewImage,
      }));

      // When the turn has no text of its own but did work, add a message with
      // the accumulated tool traces so they don't vanish when finishStream
      // clears the streaming state. Two ways to get there: a pending approval
      // paused the turn, or a card flushed the text above it and the model
      // called more tools before ending. Team reports folded so far ride along
      // — neither case may orphan a live team card.
      if (
        !fullResponse &&
        (result.hasPendingApprovals || result.flushedSegments > 0) &&
        finalToolUse.length > 0
      ) {
        store().addMessage({
          role: 'assistant',
          content: '',
          timestamp: new Date().toISOString(),
          toolUse: finalToolUse,
          teamReports: store().takeTeamReports(),
          _thinkingMs: result.thinkingMs > 0 ? Math.round(result.thinkingMs) : undefined,
          _thinkingText: result.thinkingText || undefined,
        });
      }

      // Atomically stop streaming AND add the final message in a single
      // store update so there's no render frame where neither the streaming
      // bubble nor the final message is visible (prevents flicker).
      //
      // The program_artifact event was deliberately deferred (see
      // readSSEStream) so that the artifact card renders BELOW the
      // explanation text in the same message, instead of above the text in
      // an earlier message that also re-displayed the same tool chips.
      // If the round had no text response but did emit an artifact, we
      // still synthesize a message so the artifact renders.
      let finalContent = fullResponse;
      if (!finalContent && (result.pendingArtifact || result.pendingVisuals.length > 0 || result.pendingPlan)) {
        finalContent = '';
      }
      // Move the turn-local team reports onto the message being committed —
      // this is what makes the rich TeamReportCard permanent (and persisted,
      // since chat_history serializes messages verbatim). Returns undefined
      // if an earlier commit site (approval pause) already took them.
      const turnTeamReports = store().takeTeamReports();
      const assistantMsg: ChatMessage | undefined = (finalContent || result.pendingArtifact || result.pendingVisuals.length > 0 || result.pendingPlan || turnTeamReports)
        ? {
            role: 'assistant',
            content: finalContent,
            timestamp: new Date().toISOString(),
            skills: result.skillsUsed.length > 0 ? result.skillsUsed : undefined,
            traces: result.skillTraces.length > 0 ? result.skillTraces : undefined,
            toolUse: finalToolUse.length > 0 ? finalToolUse : undefined,
            teamReports: turnTeamReports,
            programArtifact: result.pendingArtifact ?? undefined,
            visuals: result.pendingVisuals.length > 0 ? result.pendingVisuals : undefined,
            plan: result.pendingPlan ?? undefined,
            _thinkingMs: result.thinkingMs > 0 ? Math.round(result.thinkingMs) : undefined,
            _thinkingText: result.thinkingText || undefined,
            _usage: (result.inputTokens > 0 || result.outputTokens > 0)
              ? { input_tokens: result.inputTokens, output_tokens: result.outputTokens }
              : undefined,
            grounding: result.grounding,
          }
        : undefined;
      store().finishStream(assistantMsg);

      // Auto-generate the title ONCE, after the first exchange (fire-and-forget,
      // don't block UI). Reads the OWNING session's metadata, not the viewed one.
      // Skipping when a title already exists (custom or generated) keeps the name
      // stable instead of re-titling every turn.
      const msgs = store().messages;
      if (msgs.length >= 2 && activeSessionId && !isContinuation) {
        const liveSession = sessionStore().liveSessions[activeSessionId];
        if (liveSession && !liveSession.customTitle && !liveSession.generatedTitle) {
          // Role-labelled so the model titles from the user's request, not the
          // assistant's prose (mirrors how Claude names a conversation).
          const convoText = msgs
            .filter(m => m.role === 'user' || m.role === 'assistant')
            .map(m => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content}`)
            .join('\n')
            .slice(0, 2000);
          void fetch('/api/generate-title', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // session_id attributes the title call's cost to this session.
            body: JSON.stringify({ text: convoText, session_id: activeSessionId }),
          })
            .then(r => {
              // A 500 from the title endpoint still has a body
              // (often `{error: "..."}`), and parsing it as
              // `{title: string}` would assign undefined/garbage to
              // the session title. Bail early on non-2xx.
              if (!r.ok) throw new Error(`title HTTP ${r.status}`);
              return r.json() as Promise<{ title: string }>;
            })
            .then(d => { if (d.title) sessionStore().updateSessionTitle(activeSessionId, d.title, false); })
            .catch(() => { /* ignore — title is non-essential */ });
        }
      }

    } catch (err) {
      if (toError(err).name === 'AbortError') {
        // The kill switch already finalized UI state (and appended the
        // "(Stopped)" message) synchronously; only a plain re-send abort
        // still needs the fallback finalization here.
        if (!wasKillFinalized(controller)) {
          // Same commit as the kill switch: partial prose, the tool activity
          // shown so far and any live team card land as one "(Stopped)"
          // message in the same pass that clears the streaming state.
          store().finishStream(buildStoppedMessage(store()));
        }
      } else {
        console.error('[Chat] Error:', err);
        const errorMsg = err instanceof Error ? err.message : 'Chat request failed';
        store().finishStream({
          role: 'assistant',
          content: `*Error: ${errorMsg}*`,
          timestamp: new Date().toISOString(),
        });
      }
    } finally {
      releaseStreamController(activeSessionId, controller);
      // Ensure streaming is cleared (no-op if finishStream already ran)
      if (store().isStreaming) store().setStreaming(false);
      // The recorded rounds now include this turn's: their pricing note (a
      // GPT round's dated list-rate note) and estimate count reach the readout.
      void refreshSessionUsage(activeSessionId);
      // Re-focus chat input
      const chatInput = document.getElementById('chatInput') as HTMLTextAreaElement | null;
      chatInput?.focus();
    }
  }, []);

  // Send a message while THIS session's turn is already streaming. Unlike
  // `send`, this never touches the running stream's AbortController or
  // `isStreaming` — the ORIGINAL turn keeps owning both. Strictly binary, on
  // purpose: either the backend confirms it queued the text into the inbox
  // the running turn drains (server/chat/engine/midturn_inbox.py) and the
  // message appears, or nothing is shown and the caller is told it wasn't
  // delivered — never a silent middle state where the message looks sent
  // but wasn't, or quietly turns into something else. Returns whether it was
  // delivered, so the composer only clears the input on confirmed success.
  const sendMidTurn = useCallback(async (question: string): Promise<boolean> => {
    const activeSessionId = useSessionStore.getState().currentSessionId;
    if (!activeSessionId) {
      // The one path that used to return false without saying anything: from
      // the composer that looks exactly like a dead Enter key, which is how
      // this was reported. Every outcome of a send attempt now tells the user
      // what happened.
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Not delivered: this window has no active session to add the message to.',
        duration: 5000,
      });
      return false;
    }

    // An approved action is still running: no turn holds the server's slot
    // until its continuation starts, so the server would refuse this anyway.
    // Say what is actually happening instead of "no longer running".
    if (getChatStore(activeSessionId).getState().streamStatus === APPROVED_ACTION_STATUS) {
      useUIStore.getState().addToast({
        type: 'error',
        message:
          'Not sent: the approved action is still running, and the turn resumes as soon as it finishes. Send your message once the reply is streaming, or press Stop.',
        duration: 5000,
      });
      return false;
    }

    // One delivery at a time per session: a second message waits until the
    // first is confirmed or refused, so a retry can never double-queue it.
    if (midTurnInFlight.has(activeSessionId)) {
      useUIStore.getState().addToast({
        type: 'info',
        message: 'Still delivering your last message to the running turn. Your text stays in the box until it lands.',
        duration: 4000,
        key: 'midturn-in-flight',
      });
      return false;
    }
    midTurnInFlight.add(activeSessionId);

    let delivered = false;
    try {
      // Deliberately a minimal body: the backend only needs `question` +
      // `session_id` to queue it into the turn that's actually running.
      // `midturn` tells it this body may ONLY be queued: if the turn already
      // finished in the tiny window before the request arrived, the backend
      // answers 409 instead of starting a fresh turn from an incomplete
      // body, and that outcome is reported as NOT delivered.
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: activeSessionId, midturn: true }),
      });
      // Delivered only when the server SAYS it queued the text; a 200 alone
      // (or any other JSON body) is not a confirmation.
      if (response.ok && isJsonReply(response)) {
        delivered = (await readJsonReply(response))?.queued_into_running_turn === true;
      } else {
        void response.body?.cancel();
      }
    } catch {
      delivered = false;
    } finally {
      midTurnInFlight.delete(activeSessionId);
    }

    if (delivered) {
      getChatStore(activeSessionId).getState().addMessage({
        role: 'user',
        content: question,
        timestamp: new Date().toISOString(),
      });
      // Say where the message went: it is folded into the turn that is
      // already running and answered at that turn's end, not by a separate
      // reply. Without this, the silence after sending read as a lost message.
      toastQueuedIntoRunningTurn();
    } else {
      useUIStore.getState().addToast({
        type: 'error',
        message:
          'Not delivered: the server no longer counts this turn as running, so nothing was sent. Send it again as a new message, or press Stop first if the assistant still looks busy.',
        duration: 5000,
      });
    }
    return delivered;
  }, []);

  // Stop button: instant kill of the session the user is LOOKING AT (state
  // finalized synchronously) plus that session's own /subagent runs. Other
  // sessions keep streaming and keep their runs.
  const abort = useCallback(() => {
    killSessionStream(useSessionStore.getState().currentSessionId);
  }, []);

  return { send, sendMidTurn, abort };
}
