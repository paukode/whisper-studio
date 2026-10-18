/**
 * SSE stream engine for /api/chat.
 *
 * Owns readSSEStream, extracted from useChatStream.ts: parse one /api/chat
 * SSE response, dispatch every event type to the stores, and surface pending
 * approvals / user questions.
 * The approval continuation (sendApprovalContinuation: POST an approval
 * accept/deny, then feed the continuation response straight back through
 * readSSEStream) lives in ./approvalContinuation. The click-to-resume leg of
 * an approval card (running the approved action first) lives in ./approvalLeg.
 */
import { getChatStore } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import type { VizArtifact } from '@/types/chat';
import { TOAST_PRIORITY, useUIStore } from '@/stores/uiStore';
import { useTaskStore, normalizeTasks } from '@/stores/taskStore';
import { useDockStore } from '@/stores/dockStore';
import type {
  ChatMessage,
  SkillTrace,
  SSEEventData,
  TeamProgressEvent,
  ToolUseEvent,
  TurnModelSettings,
} from '@/types/chat';
import { SSEEventDataSchema } from '@/types/schemas';
import { toError } from '@/utils/toError';
import { registerPendingTextFlusher } from './streamControl';
import { createStreamPacer } from './streamPacer';
import { renderEventCards } from './sseEventCards';
import { syncAutoAppliedTab } from './autoApplied';
import { toolMetaFromFrame } from './toolMeta';
import { labelCommit, patchCommittedLabel, writtenLabelFromFrame, type WrittenLabel } from './activityLabels';

// Augment Window for SSE diagnostics access
declare global {
  interface Window { __lastSSE?: SSEEventData[]; }
}

// Keep last 200 SSE events for DevTools diagnostics. The hook's mount effect
// (see useChatStream) assigns ``window.__lastSSE`` to this live array rather
// than binding at module scope, so Vite HMR re-binding the module always
// re-attaches the *current* array to ``window`` — otherwise devtools panels
// that grabbed a reference earlier would be reading a stale array.
export const _sseEventLog: SSEEventData[] = [];

/** The key a trace's live row is updated under (see
 *  chatStore.updateStreamToolUse): the provider's id, else the name. */
const rowKey = (t: SkillTrace): string => t.id || t.name;

/**
 * The transcript row a trace commits as, shared by every commit site (the
 * mid-turn segment, the question card, the approval continuation and the
 * final message of useChatStream). The status is terminal: 'error' when the
 * server flagged the result, else 'complete' (a trace still running at
 * commit time has no result to wait for; the kill switch stamps 'stopped'
 * on its own rows in streamControl). A resolved trace also carries the
 * verdict and exit code its result brought, and the meta when it had one.
 * Every trace keeps its round, when its skill frame named one.
 */
export function committedToolUse(t: SkillTrace): ToolUseEvent {
  return {
    toolId: rowKey(t),
    toolName: t.name,
    input: t.input ?? {},
    result: t.output || undefined,
    status: t.status === 'error' ? 'error' : 'complete',
    previewImage: t.previewImage,
    ...(t.resolved ? { isError: t.status === 'error', exitCode: t.exitCode ?? null } : {}),
    ...(t.meta ? { meta: t.meta } : {}),
    ...(t.round !== undefined ? { round: t.round } : {}),
  };
}

/**
 * The round a stream's first round is stored as: one past the highest round
 * on the trailing assistant rows (the turn on screen), else 0. A stream
 * that starts a turn follows the user's row and gets 0. A leg that resumes
 * a turn (an approval continuation, an answer to a question or folder card)
 * counts on from the leg that paused. The server counts each leg's rounds
 * from 0 again, so without the base the leg's first round would take the
 * paused leg's number, and a tool-only row it commits, drawn in one list
 * with the paused row (turnEntries.traceRuns), would join that round's
 * activity group and take its caption.
 */
export function nextRoundBase(messages: readonly ChatMessage[]): number {
  let top = -1;
  for (let k = messages.length - 1; k >= 0 && messages[k].role === 'assistant'; k -= 1) {
    for (const t of messages[k].toolUse ?? []) {
      if (typeof t.round === 'number' && t.round > top) top = t.round;
    }
  }
  return top + 1;
}

/**
 * Parse an SSE stream from /api/chat and dispatch events to stores.
 *
 * `turnSettings` are the model settings the request that opened this stream
 * carried. An approval the stream pauses on keeps them, so its continuation
 * resumes the turn on the settings it ran on, not on whatever the picker
 * shows when the user answers.
 *
 * Each skill frame's round is stored plus nextRoundBase, so the rounds stay
 * unique across the legs of one turn.
 */
export async function readSSEStream(
  response: Response,
  sessionId: string,
  signal: AbortSignal,
  turnSettings: TurnModelSettings,
): Promise<{
  fullResponse: string;
  skillsUsed: string[];
  skillTraces: SkillTrace[];
  thinkingText: string;
  thinkingMs: number;
  inputTokens: number;
  outputTokens: number;
  hasPendingApprovals: boolean;
  hasUserQuestion: boolean;
  /** How many mid-turn segments were committed before a card (see
   *  `flushSegment`). Non-zero means this turn's text is ALREADY in the
   *  transcript, so callers must not treat an empty `fullResponse` as "the
   *  model said nothing" and synthesize the empty-answer fallback over it. */
  flushedSegments: number;
  /** Index-grounding summary for this turn (folders searched / passages
   *  injected, plus the persisted answer-sources id when passages exist),
   *  from the `grounding` SSE event. Undefined on resume turns. */
  grounding?: { searched: number; passages: number; id?: string };
  /** The most recent program_artifact event from this round, captured but
   *  intentionally NOT added as its own assistant message. The caller
   *  attaches it to the final assistant message so the artifact card
   *  renders below the model's explanation text and the tool traces only
   *  appear once. */
  pendingArtifact: { title: string; html: string; description: string } | null;
  /** Diagrams and charts from this round. A list, not a slot: one turn can
   *  legitimately emit several ("show me three layout options"). */
  pendingVisuals: VizArtifact[];
  pendingPlan: { id: string; title: string; summary: string } | null;
  /** The server ended the turn before it started (a refusal at model
   *  resolution, a failed setup or model load: an error frame that carries
   *  an `error_code`): its reason. Null when the turn ran. */
  refusal: string | null;
  /** Captions (activity_label frames) for calls this stream had not
   *  committed when it ended: the caller's commit takes them with
   *  labelCommit. */
  activityLabels: WrittenLabel[];
}> {
  // Parallel sessions: bind the OWNING session's store from the sessionId
  // this stream was started with. Never resolve the active session here —
  // every store() call below must keep landing in the same session no
  // matter what the user is viewing.
  const store = () => getChatStore(sessionId).getState();
  if (!response.body) throw new Error('Response body is null');
  // Read before the stream commits anything of its own.
  const roundBase = nextRoundBase(store().messages);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullResponse = '';
  let thinkingText = '';
  let thinkingMs = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  const skillsUsed: string[] = [];
  const skillTraces: SkillTrace[] = [];
  let firstTextReceived = false;
  let hasPendingApprovals = false;
  let hasUserQuestion = false;
  let grounding: { searched: number; passages: number; id?: string } | undefined;
  let pendingArtifact: { title: string; html: string; description: string } | null = null;
  const pendingVisuals: VizArtifact[] = [];
  let pendingPlan: { id: string; title: string; summary: string } | null = null;
  let flushedSegments = 0;
  let refusal: string | null = null;
  // The ask_user_question calls of this round, one card once the stream has
  // paused on them (see commitQuestions).
  const roundQuestions: Array<{ question: string; options: string[]; toolUseId: string }> = [];
  // Captions for calls still streaming: each rides on the message that
  // commits its calls (labelCommit at every commit site). The store's
  // currentStreamLabels holds them too, for the live rows.
  const pendingLabels: WrittenLabel[] = [];
  // Not const: each flushed segment restarts the thinking clock, so a "Thinking
  // 42s" after a card measures the wait since that card rather than replaying
  // the whole turn's elapsed time under every segment.
  let thinkingBlockStart = performance.now();

  // Streamed text reaches the store through pacers: one write per frame, and
  // Bedrock's clumps revealed as an even flow. `fullResponse` and
  // `thinkingText` keep every token as it arrives; they are what gets
  // committed, so a pacer only ever decides what is on screen right now.
  const textPacer = createStreamPacer((t) => store().appendStreamToken(t));
  const thinkingPacer = createStreamPacer((t) => store().appendThinkingToken(t));
  const unregisterFlusher = registerPendingTextFlusher(sessionId, () => {
    thinkingPacer.flush();
    textPacer.flush();
  });

  /**
   * Close off everything streamed so far as its own assistant message, so a
   * card appended next lands BELOW the prose and tool traces that led to it.
   *
   * Cards arrive mid-turn; the turn's text is committed only when the stream
   * ends. Left alone, that ordering is inverted on screen — the card is
   * appended immediately and the whole discussion lands underneath it, which
   * is what pushed a workflow's Approve button up out of view.
   *
   * Everything folded in here is drained: leaving it would re-commit the same
   * prose below the card at stream end and render every tool chip twice.
   * A no-op when there is nothing to commit (a card that arrives before any
   * text or tool call), so it never adds an empty row.
   */
  const flushSegment = (): void => {
    const toolUse: ToolUseEvent[] = skillTraces.map(committedToolUse);
    if (!fullResponse && !thinkingText && toolUse.length === 0) return;
    // The segment commits the full text below; drop what is still unpainted.
    textPacer.reset();
    thinkingPacer.reset();
    store().flushStreamSegment(labelCommit({
      role: 'assistant',
      content: fullResponse,
      timestamp: new Date().toISOString(),
      toolUse: toolUse.length > 0 ? toolUse : undefined,
      _thinkingMs: thinkingMs > 0 ? Math.round(thinkingMs) : undefined,
      _thinkingText: thinkingText || undefined,
    }, store().messages, pendingLabels));
    fullResponse = '';
    thinkingText = '';
    thinkingMs = 0;
    skillTraces.length = 0;
    // The next segment times its own pre-text wait from here (the store's live
    // timer was restarted in the same update by flushStreamSegment).
    thinkingBlockStart = performance.now();
    firstTextReceived = false;
    flushedSegments += 1;
  };

  // One card for the round's questions, added once the stream has paused on
  // them. Prose the model streamed before asking and the traces of the other
  // tools it ran move onto this message, so they render above the card in
  // order and are not committed a second time at stream end.
  const commitQuestions = () => {
    if (roundQuestions.length === 0) return;
    const st = store();
    // The steps already stand for every ask_user_question call of the round,
    // so their own traces would only be duplicate chips.
    const toolUseEntries: ToolUseEvent[] = skillTraces
      .filter((t) => t.name !== 'ask_user_question')
      .map(committedToolUse);
    st.addMessage(labelCommit({
      role: 'assistant',
      content: fullResponse,
      timestamp: new Date().toISOString(),
      userQuestions: [...roundQuestions],
      toolUse: toolUseEntries.length > 0 ? toolUseEntries : undefined,
      // Team activity from before the questions stays with its traces on
      // this message, and the deliveries with the prose.
      teamReports: st.takeTeamReports(),
      deliveries: st.takeDeliveries(),
      _thinkingMs: thinkingMs > 0 ? Math.round(thinkingMs) : undefined,
      _thinkingText: thinkingText || undefined,
      // The answer resumes this turn on the settings it ran on.
      turnSettings,
    }, st.messages, pendingLabels));
    roundQuestions.length = 0;
    fullResponse = '';
    skillTraces.length = 0;
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (signal.aborted) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const data = trimmed.slice(6);
        if (data === '[DONE]') continue;

        try {
          const raw: unknown = JSON.parse(data);
          const validated = SSEEventDataSchema.safeParse(raw);
          // When validation fails we still pass the raw payload through (some
          // SSE event types are intentionally loose Record<string, unknown>),
          // but we log loudly. The old silent fallback hid malformed
          // approval_request events that then crashed the banner.
          if (!validated.success) {
            console.warn('SSE: payload failed Zod validation', { issues: validated.error.issues, raw });
          }
          const parsed: SSEEventData = validated.success ? validated.data : (raw as SSEEventData);

          // SSE diagnostics
          _sseEventLog.push(parsed);
          if (_sseEventLog.length > 200) _sseEventLog.shift();
          store().incrementSseCount();

          // ── grounding (index-search summary for this turn) ──
          if (parsed.grounding) {
            grounding = parsed.grounding;
          }

          // ── skill (tool trace start) ──
          if (parsed.skill) {
            // A tool call proves the model is responding: the setup status is
            // stale now. Models that open with tool calls and no thinking or
            // text (GPT rounds routinely do) otherwise keep the header on
            // "Waiting for the model…" through the whole tool-running phase.
            if (store().streamStatus) {
              store().setStreamStatus(null);
            }
            // Prose that was streamed BEFORE this tool call is a finished
            // thought; commit it as its own row so the text the model writes
            // after the tool work lands as a separate, later row (the turn's
            // bubble, TurnBubble, still shows the rows together, in order).
            // Concatenating both into one reply produced "No. ... Yes, it's
            // saved now." in a single message. Tool-only segments are left
            // to accumulate (no text = nothing to separate).
            if (fullResponse) flushSegment();
            // Remove empty thinking element if no thinking content
            if (!thinkingText) {
              store().appendThinkingToken(''); // signal no thinking
            }
            skillsUsed.push(parsed.skill);
            // The provider's tool_use id pairs the call's later frames with
            // this trace; '' when the provider could not tag the frame, in
            // which case those frames pair by name order instead.
            const id = parsed.tool_use_id || '';
            // The model round that issued the call: each round's calls draw
            // as an activity group of their own, live and once committed.
            // Rounds count from 0, so the check is on the type. The base keeps
            // a resumed leg's rounds after the paused leg's.
            const round = typeof parsed.round === 'number' ? { round: roundBase + parsed.round } : {};
            const trace: SkillTrace = { id, name: parsed.skill, input: {}, output: '', status: 'running', ...round };
            skillTraces.push(trace);
            // Show trace in real-time during streaming
            store().addStreamToolUse({
              toolId: rowKey(trace),
              toolName: parsed.skill,
              input: {},
              status: 'running',
              ...round,
            });
          }

          // ── skill_input ──
          // By id when the frame carries one; an untagged frame belongs to
          // the call that started last.
          if (parsed.skill_input && parsed.input) {
            const trace = parsed.tool_use_id
              ? skillTraces.find((t) => t.id === parsed.tool_use_id)
              : skillTraces[skillTraces.length - 1];
            if (trace) {
              trace.input = parsed.input;
              store().updateStreamToolUse(
                rowKey(trace),
                { input: parsed.input as Record<string, unknown> },
              );
            }
          }

          // ── skill_progress (arguments still streaming) ──
          // A whole HTML app arrives as one tool call's arguments and can take
          // minutes; without this the running card looked hung the entire time.
          // By id when the frame carries one, else the first running call of
          // that name.
          if (parsed.skill_progress) {
            const sp = parsed.skill_progress;
            const trace = sp.tool_use_id
              ? skillTraces.find((t) => t.id === sp.tool_use_id)
              : skillTraces.find((t) => t.name === sp.name && t.status === 'running');
            store().updateStreamToolUse(trace ? rowKey(trace) : sp.name, {
              progressChars: sp.chars,
            });
          }

          // ── skill_result (tool complete) ──
          if (parsed.skill_result) {
            const toolName = parsed.skill_result;
            const toolOutput = parsed.output ?? '';
            // The server's verdict on the full output (one rule, shared with
            // its loop guard); the client never guesses from the text.
            const isError = parsed.is_error === true;
            const exitCode = parsed.exit_code ?? null;
            // What the server measured (duration, size, a write's diff).
            const meta = toolMetaFromFrame(parsed.meta);
            store().setLastTool(toolName, toolOutput, isError);

            const previewImage = parsed.preview_image;

            // By id when the frame carries one. An untagged frame matches the
            // FIRST not-yet-resolved trace with its name: such tools complete
            // in order, so this assigns each skill_result to its own call
            // (matching purely by name overwrote the first trace's output for
            // every repeated same-named call, leaving the rest empty).
            const trace = parsed.tool_use_id
              ? skillTraces.find((t) => t.id === parsed.tool_use_id)
              : skillTraces.find((t) => t.name === toolName && !t.resolved);
            if (trace) {
              trace.output = toolOutput;
              trace.resolved = true;
              trace.status = isError ? 'error' : 'complete';
              trace.exitCode = exitCode;
              if (meta) trace.meta = meta;
              if (previewImage) trace.previewImage = previewImage;
            }

            // Update real-time trace to complete
            store().updateStreamToolUse(trace ? rowKey(trace) : parsed.tool_use_id || toolName, {
              status: isError ? 'error' : 'complete',
              result: toolOutput,
              isError,
              exitCode,
              ...(meta ? { meta } : {}),
              ...(previewImage ? { previewImage } : {}),
            });

            // Refresh git panel after git write operations
            if (toolName.startsWith('git_') &&
                !['git_status','git_diff','git_log','git_branch_list','git_show','git_blame','git_stash_list'].includes(toolName)) {
              // Dispatch custom event for git panel refresh
              window.dispatchEvent(new CustomEvent('whisper-git-refresh'));
            }

            // Refresh workspace file tree after any workspace-mutating tool.
            // ws_auto_applied and the approval continuation already dispatch
            // this for tools that go through the approval pipeline; firing
            // here covers tools that bypass that path (e.g. acceptEdits or
            // bypassPermissions modes where writes complete inline) and is
            // a no-op for read-only tools because the listener is debounced.
            if (!isError && (toolName === 'ws_create_file' || toolName === 'ws_write_file')) {
              window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
            }
          }

          // ── activity_label (a model-written caption for a group of calls) ──
          // Calls still streaming show it on their live row now and keep it
          // for the message that commits them; calls already committed get
          // it on their row now, saved.
          if (parsed.activity_label) {
            const written = writtenLabelFromFrame(parsed.activity_label);
            if (written && skillTraces.some((t) => t.id && written.ids.includes(t.id))) {
              pendingLabels.push(written);
              store().addStreamLabel(written);
            } else if (written) {
              patchCommittedLabel(sessionId, written);
            }
          }

          renderEventCards(parsed, store, sessionId, flushSegment);
          // ── ws_auto_applied (pre-approved action executed server-side) ──
          if (parsed.ws_auto_applied) {
            // Refresh workspace tree
            window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
            // Show the write in its open tab, then what is on disk (ruff may
            // have fixed and formatted a Python file after the write).
            const autoApplied = parsed.ws_auto_applied;
            if (autoApplied.path) {
              syncAutoAppliedTab(autoApplied.path, autoApplied.content, autoApplied.workspace_root);
            }
          }

          // ── ws_workspace_prompt ──
          if (parsed.ws_workspace_prompt) {
            hasPendingApprovals = true;
            // server/tool_executor.py merges the real tool_use_id into this
            // payload before emitting it (see prompt_parsed = {**prompt_parsed,
            // "tool_use_id": state.tool_id}). Use it as the toolId so the
            // resume flow (WorkspacePromptCard) can answer the ACTUAL paused
            // tool_use block via the answers-array continuation path instead
            // of a hardcoded id that never matches anything server-side.
            // Fall back to the old hardcoded string only if some older/odd
            // payload shape omits the field.
            const wp = parsed.ws_workspace_prompt as Record<string, unknown>;
            const toolUseId =
              typeof wp.tool_use_id === 'string' && wp.tool_use_id
                ? wp.tool_use_id
                : 'ws_workspace_prompt';
            // Same ordering rule as the event cards: commit the prose that led
            // up to the picker first, so the folder buttons sit at the bottom
            // of the conversation where the user is already looking.
            flushSegment();
            store().addMessage({
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString(),
              toolUse: [{
                toolId: toolUseId,
                toolName: 'ws_workspace_prompt',
                input: parsed.ws_workspace_prompt,
                status: 'pending',
              }],
              // The folder choice resumes this turn on the settings it ran on.
              turnSettings,
            });
          }

          // ── approval_request (generic, declarative) ──
          if (parsed.approval_request) {
            hasPendingApprovals = true;
            const req = parsed.approval_request;

            // Backend doesn't emit skill_result for tools that pause for
            // approval; mark the paused call complete so the UI doesn't show
            // a spinning skill forever. The request names the call by its
            // tool_use_id; the last running row is the fallback for a row
            // from an untagged frame. Only an unresolved trace can be the
            // paused one: matching by name alone used to overwrite the
            // output of an earlier, finished call of the same tool.
            const reqId = req.tool_use_id;
            const liveRows = store().currentStreamToolUse;
            const paused =
              (reqId ? liveRows.find((t) => t.toolId === reqId && t.status === 'running') : undefined)
              ?? [...liveRows].reverse().find((t) => t.status === 'running');
            if (paused) {
              store().updateStreamToolUse(paused.toolId, {
                status: 'complete',
                result: `Awaiting approval: ${req.summary}`,
              });
              const localTrace =
                (reqId ? skillTraces.find((t) => t.id === reqId && !t.resolved) : undefined)
                ?? skillTraces.find((t) => t.name === paused.toolName && !t.resolved);
              if (localTrace) localTrace.output = `Awaiting approval: ${req.summary}`;
            }

            const approval: PendingApproval = {
              toolUseId: req.tool_use_id,
              action: req.action,
              category: req.category,
              preview: req.preview,
              summary: req.summary,
              payload: req.payload as Record<string, unknown>,
              riskHint: req.risk_hint ?? null,
              explanation: req.explanation ?? null,
              alwaysAsks: req.always_asks === true,
              sessionId,
              turnSettings,
            };

            // Always a card. The server already applied this session's "Yes,
            // all" / "Block" memory (session_approvals rides on every request,
            // server/security/permissions.py), so a request that still arrives
            // is one it insists a human answers: rm, a destructive GitHub
            // call, a sandbox escalation, an MCP server marked approve. Running
            // it here on the remembered "allow" bypassed exactly those floors,
            // and did so in a detached task after this stream had closed, so
            // the session looked idle while the action ran and its resume
            // raced any new turn.
            store().enqueueApproval(approval);
          }

          // ── user_question (ask_user tool) ──
          // The round's ask_user_question calls arrive as consecutive frames
          // and the server pauses the turn right after them. They are kept
          // until the stream ends and then become ONE card (commitQuestions),
          // so a pick can never answer a round whose other questions are
          // still on the wire. A question in a later stream is a new round
          // and gets its own card. (The singular `userQuestion` field is
          // read-only tolerance for old persisted rows; nothing writes it.)
          if (parsed.user_question) {
            hasUserQuestion = true;
            const uq = parsed.user_question as { question?: string; options?: string[]; tool_use_id?: string };
            roundQuestions.push({
              question: uq.question ?? '',
              options: uq.options ?? [],
              toolUseId: uq.tool_use_id ?? '',
            });
          }

          // ── program_artifact (create_artifact tool) ──
          //
          // We do NOT add a separate assistant message here, even though the
          // artifact data has arrived. Two reasons:
          //   1. Bedrock continues to stream text after the tool_use call,
          //      so the model's explanation text comes AFTER the artifact
          //      event. Reading flow is "explanation → artifact", which
          //      is the natural order. If we add a message now, the card
          //      lands above the explanation in chat history.
          //   2. The accumulated skillTraces were going to be attached
          //      twice — once on the early artifact message, once on the
          //      final text message — which renders the same `web_fetch`
          //      and `create_artifact` tool chips on two consecutive cards.
          //
          // Instead, stash the artifact and let the final-message path
          // attach it. The user sees the artifact card render right after
          // the explanation text settles, at the bottom of one cohesive
          // assistant message.
          if (parsed.program_artifact) {
            const pa = parsed.program_artifact as {
              title?: string;
              html?: string;
              description?: string;
            };
            pendingArtifact = {
              title: pa.title ?? 'Untitled Program',
              html: pa.html ?? '',
              description: pa.description ?? '',
            };
          }

          // ── viz_artifact (create_visual / create_chart) ──
          // Deferred to the final message for the same reason as artifacts:
          // the model's explanation streams after the tool call, and the
          // picture belongs below the sentence that introduces it.
          if (parsed.viz_artifact) {
            const va = parsed.viz_artifact as {
              kind?: string;
              title?: string;
              description?: string;
              source?: string;
            };
            if (va.source) {
              pendingVisuals.push({
                kind: va.kind === 'chart' ? 'chart' : 'svg',
                title: va.title ?? 'Visual',
                description: va.description ?? '',
                source: va.source,
              });
            }
          }

          // ── plan_generated (create_plan tool) ──
          // Stash for the final message (like the artifact) AND open the plan
          // in the dock immediately so the user sees it as soon as it's saved.
          if (parsed.plan_generated) {
            const pg = parsed.plan_generated as { id?: string; title?: string; summary?: string };
            pendingPlan = { id: pg.id ?? '', title: pg.title ?? 'Plan', summary: pg.summary ?? '' };
            if (pg.id) {
              useDockStore.getState().openPanel({
                id: `plan:${pg.id}`, kind: 'plan', title: pg.title ?? 'Plan', meta: { planId: pg.id },
              });
            }
          }

          // ── ws_folder_opened ──
          if (parsed.ws_folder_opened) {
            useUIStore.getState().setWsConnected(true, parsed.ws_folder_opened);
            window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
          }

          // ── notify_user ──
          if (parsed.notify_user) {
            const n = parsed.notify_user as Record<string, string>;
            const msg = n.message ?? '';
            // Honor the tool's declared status (success/warning/error) and title;
            // warnings and errors linger longer.
            const type =
              n.status === 'success' || n.status === 'warning' || n.status === 'error'
                ? n.status
                : ('info' as const);
            const duration = type === 'warning' || type === 'error' ? 9000 : 5000;
            // persist:false — notify_user is already recorded in the durable
            // log at the route_tool choke point; persisting here would
            // double-log every notification under a second source.
            useUIStore
              .getState()
              .addToast({ type, title: n.title || undefined, message: msg, duration, persist: false });
          }

          // ── status (mid-turn progress notice, e.g. "Compacting context…") ──
          // Emitted by server/chat/routes.py when it has to react mid-turn
          // (reactive context compaction on a PromptTooLong retry). It rides
          // through the Zod passthrough (not on the SSEEventData type), so read
          // it off a narrowed view. Surface a transient info toast keyed so
          // repeated notices coalesce instead of stacking, and keep draining
          // the stream — the turn continues after the notice.
          const statusMsg = (parsed as SSEEventData & { status?: string }).status;
          if (typeof statusMsg === 'string' && statusMsg) {
            useUIStore.getState().addToast({
              type: 'info',
              message: statusMsg,
              duration: 4000,
              key: 'stream-status',
              // Mid-turn progress notice: transient by nature, never logged.
              persist: false,
            });
          }

          // ── budget_warning (session/daily cost budget tripped) ──
          // Emitted by server/chat/routes.py right before the turn stops when a
          // configured cost limit is reached. These fields ride through the Zod
          // passthrough (they are not on the SSEEventData type), so read them
          // off a narrowed view. Surface a persistent error toast — the plain
          // "[Budget exceeded] …" text frame alone is easy to miss. Do NOT
          // break: the stream still needs to drain the trailing text/[DONE].
          const budget = parsed as SSEEventData & {
            budget_warning?: string;
            budget_kind?: string;
            budget_limit?: number;
            budget_current?: number;
          };
          if (budget.budget_warning) {
            const kind = budget.budget_kind ?? 'cost';
            const limit = budget.budget_limit;
            const current = budget.budget_current;
            // The backend message already spells out kind/limit/current; keep a
            // composed fallback that also carries all three so the toast is
            // meaningful even if the message string is ever empty.
            const fallback =
              typeof current === 'number' && typeof limit === 'number'
                ? `${kind} cost $${current.toFixed(4)} reached the $${limit.toFixed(2)} limit.`
                : `${kind} budget limit reached.`;
            useUIStore.getState().addToast({
              type: 'error',
              message: budget.budget_warning || fallback,
              // Persistent (0 = no auto-dismiss): the run stopped, so the user
              // must see it and act (new session / raise the limit).
              duration: 0,
              priority: TOAST_PRIORITY.high,
              key: 'budget-warning',
              source: 'chat',
            });
          }

          // ── auto_mode_breaker (auto-mode's classifier circuit breaker) ──
          // Emitted once by server/tool_executor.py when the rolling denial
          // counter (server/security/permissions.py) trips: auto mode stops
          // consulting the classifier and falls back to asking for the rest
          // of this turn. Surfaced as a persistent banner (not a toast — the
          // user needs a one-click way to resume, see AutoModeBreakerBanner)
          // rather than dropped once the stream ends.
          if (parsed.auto_mode_breaker) {
            store().setAutoModeBreaker({
              reason:
                parsed.auto_mode_breaker.reason
                || 'Auto mode paused after repeated confirmations this turn.',
              sessionId,
            });
          }

          // ── turn_downgrade (this turn isn't what the composer shows) ──
          // Emitted once by server/chat/routes.py when a budget fallback
          // swapped the model and/or the resolved model couldn't honour the
          // requested effort. Without this the turn silently runs cheaper
          // and shallower while the chips still read what the user picked.
          if (parsed.turn_downgrade) {
            const d = parsed.turn_downgrade;
            const parts: string[] = [];
            if (d.model_changed) parts.push(`model ${d.requested_model} → ${d.effective_model}`);
            if (d.effort_changed) parts.push(`effort ${d.requested_effort} → ${d.effective_effort}`);
            useUIStore.getState().addToast({
              type: 'info',
              title: 'This turn was downgraded',
              message: `${parts.join(', ')} (${d.reason}).`,
              duration: 8000,
              persist: false,
            });
          }

          // ── todo_update (full session task list on every task change) ──
          // The backend (server/tool_router.py) emits the whole session list on
          // every task_* call; the Tasks pane + the in-chat task row read this
          // store, so the plan stays a single live source of truth.
          if (parsed.todo_update) {
            useTaskStore.getState().setTasks(sessionId, normalizeTasks(parsed.todo_update));
          }

          // ── team_progress (live per-agent events) ──
          // Each event from server/agents/runtime.py._emit lands here.
          // During a turn no assistant message exists yet (tool traces are
          // committed at stream end), so folding into "the last message"
          // silently dropped every event. Fold into the turn-local report
          // in the chat store instead: StreamingMessage renders it live and
          // the commit sites move it onto the final assistant message.
          if (parsed.team_progress) {
            store().foldTeamEvent(parsed.team_progress as TeamProgressEvent);
          }

          // ── deliveries (claims the server verified, one frame a sentence) ──
          // Held with the live segment the same way: StreamingMessage shows
          // the chips under the text, and the message committing it takes them.
          if (parsed.deliveries) {
            store().addLiveDeliveries(parsed.deliveries.items);
          }

          // ── team_results ──
          // The final structured payload from execute_team_create. Fold it
          // into the same turn-local report; it reaches the conversation on
          // the committed assistant message alongside the team_create trace,
          // where the agent rows render it. (The old fallback that created a
          // standalone JSON message here is gone — that was the ghost
          // "Agent activity" card.)
          if (parsed.team_results) {
            store().foldTeamResults(parsed.team_results);
          }

          // ── cron_event (live cron card) ──
          // Emitted by server/cron_scheduler.py when a cron is created,
          // deleted, or fires in the background. We append a fresh
          // ChatMessage with role='cron_event' carrying the structured
          // payload — same shape the backend persists into chat_history,
          // so the live-push path and the resume path render identically.
          if (parsed.cron_event) {
            const payload = parsed.cron_event;
            const st = store();
            // A created/updated/deleted row is this turn's own tool call
            // landing, so it belongs after the sentence that announced it —
            // flush like the event cards do. `cron_fired` is NOT flushed: a
            // background firing is timed by the scheduler, not the model, and
            // splitting the reply wherever it happens to land could tear a
            // paragraph (or a fenced code block) in half.
            if (payload.event_type !== 'cron_fired') flushSegment();
            st.addMessage({
              role: 'cron_event',
              content: '',
              timestamp: payload.timestamp ?? new Date().toISOString(),
              cronEvent: payload,
            });
          }

          // ── session_message (live cross-session message) ──
          // Emitted by server/agent_tools/cross_session.py's
          // send_session_message when another session messages this one
          // while this session's turn is actively streaming. Same nested
          // shape the backend persists into chat_history, so live-push and
          // resume render identically (see SessionMessageCard).
          if (parsed.session_message) {
            const payload = parsed.session_message;
            store().addMessage({
              role: 'session_message',
              content: '',
              timestamp: payload.timestamp ?? new Date().toISOString(),
              sessionMessage: payload,
            });
          }

          // ── tool_result_truncated ──
          // Backend persists oversize tool outputs to data/result_cache/ and
          // emits this event so the UI can tell the user the model saw a
          // trimmed head+tail slice. Toast links to the full output via
          // /api/result-cache/{filename}.
          if (parsed.tool_result_truncated) {
            const t = parsed.tool_result_truncated as {
              tool_name?: string;
              full_size?: number;
              kept_bytes?: number;
              cache_filename?: string | null;
              cache_path?: string | null;
            };
            const tool = t.tool_name ?? 'tool';
            const sizeKb = t.full_size ? Math.round(t.full_size / 1024) : null;
            const keptKb = t.kept_bytes ? Math.round(t.kept_bytes / 1024) : null;
            const sizeText = sizeKb !== null ? ` produced ~${sizeKb} KB` : ' produced a large output';
            const keptText =
              keptKb !== null
                ? `a trimmed ~${keptKb} KB (head+tail) was sent to the model`
                : 'a trimmed slice was sent to the model';
            useUIStore.getState().addToast({
              type: 'info',
              message: `Tool "${tool}"${sizeText}; ${keptText}.`,
              duration: 8000,
              key: `truncate-${tool}`,
              source: 'chat',
              ...(t.cache_filename
                ? {
                    action: {
                      label: 'View full output',
                      href: `/api/result-cache/${encodeURIComponent(t.cache_filename)}`,
                    },
                  }
                : {}),
            });
          }

          // ── status (turn setup phase, before any model output) ──
          if (parsed.status) {
            store().setStreamStatus(parsed.status);
          }

          // ── thinking_start ──
          if (parsed.thinking_start) {
            store().setStreamStatus(null);
            store().setThinkingStart();
          }

          // ── thinking (content) ──
          if (parsed.thinking) {
            store().setStreamStatus(null);
            thinkingText += parsed.thinking;
            thinkingPacer.push(parsed.thinking);
          }

          // ── thinking_stop ──
          if (parsed.thinking_stop) {
            thinkingMs = thinkingBlockStart ? performance.now() - thinkingBlockStart : 0;
            store().setThinkingStop();
          }

          // ── usage ──
          // The backend (server/chat/engine/runner.py) sends BOTH per-round
          // counts (`input_tokens`/`output_tokens`) and cumulative running
          // totals (`total_prompt`/`total_input`/`total_output`) on every usage
          // frame, and its `estimated_cost_usd` is cumulative too. Prefer the
          // totals so the counter reflects the whole turn instead of collapsing
          // to the last round; fall back to per-round for any emitter that
          // omits totals.
          //
          // `total_prompt` is the input side: it counts every prompt token sent,
          // where `total_input` counts only the portion that missed the prompt
          // cache — a few tokens on a warm turn, which is what made the counter
          // read "4 in / 4,012 out". Older emitters that predate total_prompt
          // fall back to total_input.
          if (parsed.usage) {
            inputTokens =
              parsed.usage.total_prompt ??
              parsed.usage.total_input ??
              parsed.usage.input_tokens ??
              inputTokens;
            outputTokens = parsed.usage.total_output ?? parsed.usage.output_tokens ?? outputTokens;
            const cost = parsed.usage.estimated_cost_usd ?? 0;
            // context_used/context_max are real per-round token counts (WS-F);
            // feed them to the status bar's context meter.
            const ctxUsed = parsed.usage.context_used;
            const ctxMax = parsed.usage.context_max;
            store().setUsage(inputTokens, outputTokens, cost, ctxUsed, ctxMax);
          }

          // ── text ──
          if (parsed.text) {
            if (!firstTextReceived) {
              firstTextReceived = true;
              store().setStreamStatus(null);
              thinkingMs = thinkingBlockStart ? performance.now() - thinkingBlockStart : thinkingMs;
              store().setThinkingElapsed(thinkingMs);
            } else if (store().streamStatus) {
              // A mid-turn status (continuation round, compaction) is stale
              // the moment text resumes streaming.
              store().setStreamStatus(null);
            }
            fullResponse += parsed.text;
            textPacer.push(parsed.text);
          }

          // ── error ──
          if (parsed.error) {
            console.error('Backend stream error:', parsed.error);
            // Surface it in the message instead of swallowing it: an errored
            // turn with no text otherwise falls through to the generic "ended
            // turn without text" fallback, hiding the real cause (e.g. a local
            // model that failed to load or download).
            const errLine = `${fullResponse ? '\n\n' : ''}*(Error: ${parsed.error})*`;
            fullResponse += errLine;
            textPacer.flush();
            store().appendStreamToken(errLine);
            if (parsed.error_code && refusal === null) refusal = parsed.error;
          }

        } catch (e) {
          console.warn('SSE parse error:', toError(e).message, 'line:', data);
        }
      }
    }
  } finally {
    // The card is what the server sent before it paused, so it is kept even
    // when the connection then failed; only a Stop (aborted signal) drops it.
    if (!signal.aborted) commitQuestions();
    // The whole reply is on screen before the caller commits it.
    thinkingPacer.flush();
    textPacer.flush();
    unregisterFlusher();
    reader.releaseLock();
  }

  return {
    fullResponse,
    skillsUsed,
    skillTraces,
    thinkingText,
    thinkingMs,
    inputTokens,
    outputTokens,
    hasPendingApprovals,
    hasUserQuestion,
    flushedSegments,
    grounding,
    pendingArtifact,
    pendingVisuals,
    pendingPlan,
    refusal,
    activityLabels: pendingLabels,
  };
}
