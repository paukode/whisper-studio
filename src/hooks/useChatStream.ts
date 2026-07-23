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
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useIndexSearchStore } from '@/stores/indexSearchStore';
import type { ChatMessage, ToolUseEvent } from '@/types/chat';
import { toError } from '@/utils/toError';
import { ensureRetentionEnabled } from '@/components/chat/dataRetentionConsent';
import {
  _sseEventLog,
  readSSEStream,
  sendApprovalContinuation,
} from './chatStream/sseStream';
import {
  abortSessionStream,
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
      });
      return;
    }

    // Abort any in-flight stream FOR THIS SESSION only.
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

    store().setStreaming(true);

    const controller = new AbortController();
    registerStreamController(activeSessionId, controller);

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
    const allMessages = store().messages;
    const history = allMessages
      .slice(0, isContinuation ? allMessages.length : -1)
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({
        role: m.role,
        content: m.content,
      }));
    // Cap to 40 entries
    const cappedHistory = history.length > 40 ? history.slice(-40) : history;

    // Unified "Response length" control (Brief/Normal/Detailed) is stored as
    // verbosity (low/medium/high). Apply it per model: GPT-5.x uses text.verbosity
    // natively, so it gets the value as-is and no brief instruction; models
    // without native verbosity get a concise-instruction (brief_mode) only at
    // the Brief end, with verbosity ignored server-side.
    const _model = settings.models?.find((m) => m.key === settings.selectedModel);
    const _supportsVerbosity = !!_model?.supports_verbosity;
    const body: Record<string, unknown> = {
      question,
      transcript,
      history: cappedHistory,
      attachment_ids: opts?.attachmentIds ?? [],
      model: settings.selectedModel,
      force_skill: opts?.forceSkill ?? null,
      session_id: activeSessionId,
      brief_mode: _supportsVerbosity ? false : settings.verbosity === 'low',
      effort_level: settings.effortLevel,
      verbosity: settings.verbosity,
      local_thinking: settings.localThinking || false,
      // Tools on iff a scope is selected; the scope picks which tools (Off/Core/
      // Core+web/All). local_tools stays for the on/off gate; local_tool_scope
      // filters the pool server-side.
      local_tools: settings.localToolScope !== 'off',
      local_tool_scope: settings.localToolScope,
      session_denials: store().sessionDenials,
      session_approvals: store().sessionApprovals,
      approved_tool_result: opts?.approvedToolResult ?? null,
      // Indexes the user selected for this session (point D). Send the stored
      // selection verbatim; when there's no entry yet (a brand-new session whose
      // id was just minted here, so ChatInput's seeding effect hasn't run), send
      // `undefined` — JSON.stringify drops the key, and the backend treats an
      // ABSENT field as "all indexed folders" so the first question still grounds.
      // An explicit empty array (user deselected all) is preserved and means
      // "search nothing". Previously this defaulted to `[]`, which silently
      // disabled grounding on the first turn of every new session.
      selected_search_indexes:
        useIndexSearchStore.getState().selectionBySession[activeSessionId],
    };
    // Per-request MCP override. When the chat-toolbar checklist has been
    // MCP servers are resolved by the backend from each server's persisted
    // `enabled` flag (toggled live from the Settings panel or chat toolbar via
    // useMcpToggle), so we don't send a per-request override.

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
        });
        return;
      }
      if (!response.ok || !response.body) {
        throw new Error(`Chat request failed: ${response.status}`);
      }

      const result = await readSSEStream(response, activeSessionId, controller.signal);
      // A kill switch may have finalized this stream while the read loop was
      // draining (signal.aborted exits the loop NORMALLY, landing here on the
      // success path) — appending anything now would resurrect the answer.
      if (wasKillFinalized(controller)) return;
      fullResponse = result.fullResponse;

      // Empty-bubble fallback (matching original chat-stream.js)
      // Skip when a user_question was emitted — the question card IS the response
      if (!fullResponse && !result.hasPendingApprovals && !result.hasUserQuestion) {
        const { lastToolError, lastToolName, lastToolOutput, sseEventCount } = store();
        if (lastToolError) {
          fullResponse = `**${lastToolName ?? 'tool'}** returned an error:\n\n\`\`\`\n${lastToolError}\n\`\`\`\n\n*(The model sent no text response. This usually means it stopped after seeing the tool error. Ask again or rephrase.)*`;
        } else if (lastToolOutput) {
          const preview = lastToolOutput.length > 800 ? lastToolOutput.substring(0, 800) + '…' : lastToolOutput;
          fullResponse = `*(No text response from the model.)*\n\nLast tool: **${lastToolName ?? '(unknown)'}**\n\`\`\`\n${preview}\n\`\`\``;
        } else if (sseEventCount > 0) {
          fullResponse = `*(The model ended the turn without text or tool calls. SSE events received: ${sseEventCount}. Inspect \`window.__lastSSE\` in DevTools for details.)*`;
        }
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

      // When pending approvals exist but no text response, add a message
      // with the accumulated tool traces so they don't vanish when
      // finishStream clears the streaming state. Team reports folded so far
      // ride along — the approval pause must not orphan a live team card.
      if (!fullResponse && result.hasPendingApprovals && finalToolUse.length > 0) {
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
      if (!finalContent && (result.pendingArtifact || result.pendingPlan)) {
        finalContent = '';
      }
      // Move the turn-local team reports onto the message being committed —
      // this is what makes the rich TeamReportCard permanent (and persisted,
      // since chat_history serializes messages verbatim). Returns undefined
      // if an earlier commit site (approval pause) already took them.
      const turnTeamReports = store().takeTeamReports();
      const assistantMsg: ChatMessage | undefined = (finalContent || result.pendingArtifact || result.pendingPlan || turnTeamReports)
        ? {
            role: 'assistant',
            content: finalContent,
            timestamp: new Date().toISOString(),
            skills: result.skillsUsed.length > 0 ? result.skillsUsed : undefined,
            traces: result.skillTraces.length > 0 ? result.skillTraces : undefined,
            toolUse: finalToolUse.length > 0 ? finalToolUse : undefined,
            teamReports: turnTeamReports,
            programArtifact: result.pendingArtifact ?? undefined,
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
            body: JSON.stringify({ text: convoText }),
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
          // Capture content BEFORE clearing streaming state, then finish atomically
          const { currentStreamContent, currentThinkingContent, thinkingElapsedMs } = store();
          // Keep whatever team activity was already folded — an aborted turn
          // should leave the partial team card in place, not erase it.
          const abortTeamReports = store().takeTeamReports();
          const abortMsg: ChatMessage | undefined = (currentStreamContent || abortTeamReports)
            ? {
                role: 'assistant',
                content: currentStreamContent ? currentStreamContent + '\n\n*(Stopped)*' : '*(Stopped)*',
                timestamp: new Date().toISOString(),
                teamReports: abortTeamReports,
                _thinkingMs: thinkingElapsedMs > 0 ? Math.round(thinkingElapsedMs) : undefined,
                _thinkingText: currentThinkingContent || undefined,
              }
            : undefined;
          store().finishStream(abortMsg);
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
      // Re-focus chat input
      const chatInput = document.getElementById('chatInput') as HTMLTextAreaElement | null;
      chatInput?.focus();
    }
  }, []);

  // Stop button: instant kill of the session the user is LOOKING AT (state
  // finalized synchronously) plus every running subagent. Background
  // sessions keep streaming.
  const abort = useCallback(() => {
    killSessionStream(useSessionStore.getState().currentSessionId);
  }, []);

  return { send, abort };
}

// Re-export for use in approval components
export { sendApprovalContinuation };
