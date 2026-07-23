/**
 * SSE stream engine for /api/chat.
 *
 * Owns the two mutually-recursive stream functions extracted from
 * useChatStream.ts:
 *   - readSSEStream: parse one /api/chat SSE response, dispatch every event
 *     type to the stores, and surface pending approvals / user questions.
 *   - sendApprovalContinuation: POST an approval accept/deny, then feed the
 *     continuation response straight back through readSSEStream.
 * They call each other (an approval re-enters the stream), so they share a
 * module to keep the import graph acyclic.
 */
import { executeApproval } from '@/api/approval';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import type { PendingApproval } from '@/stores/chatStore';
import { TOAST_PRIORITY, useUIStore } from '@/stores/uiStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useTaskStore, normalizeTasks } from '@/stores/taskStore';
import { useDockStore } from '@/stores/dockStore';
import type {
  ChatMessage,
  SkillTrace,
  SSEEventData,
  TeamProgressEvent,
  ToolUseEvent,
} from '@/types/chat';
import { SSEEventDataSchema } from '@/types/schemas';
import { toError } from '@/utils/toError';
import { registerStreamController, releaseStreamController } from './streamControl';
import { renderEventCards } from './sseEventCards';

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

/**
 * Parse an SSE stream from /api/chat and dispatch events to stores.
 */
export async function readSSEStream(
  response: Response,
  sessionId: string,
  signal: AbortSignal,
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
  /** Index-grounding summary for this turn (folders searched / passages
   *  injected), from the `grounding` SSE event. Undefined on resume turns. */
  grounding?: { searched: number; passages: number };
  /** The most recent program_artifact event from this round, captured but
   *  intentionally NOT added as its own assistant message. The caller
   *  attaches it to the final assistant message so the artifact card
   *  renders below the model's explanation text and the tool traces only
   *  appear once. */
  pendingArtifact: { title: string; html: string; description: string } | null;
  pendingPlan: { id: string; title: string; summary: string } | null;
}> {
  // Parallel sessions: bind the OWNING session's store from the sessionId
  // this stream was started with. Never resolve the active session here —
  // every store() call below must keep landing in the same session no
  // matter what the user is viewing.
  const store = () => getChatStore(sessionId).getState();
  if (!response.body) throw new Error('Response body is null');
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
  let grounding: { searched: number; passages: number } | undefined;
  let pendingArtifact: { title: string; html: string; description: string } | null = null;
  let pendingPlan: { id: string; title: string; summary: string } | null = null;
  const thinkingBlockStart = performance.now();

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

          // ── resolved_content ──
          // The backend still emits this frame; it is intentionally ignored
          // here (no consumer reads a resolved-content value). The Zod schema
          // keeps the field optional so an incoming frame validates without
          // warning; this branch is a documented no-op passthrough.
          if (parsed.resolved_content) {
            /* intentionally ignored; see note above */
          }

          // ── grounding (index-search summary for this turn) ──
          if (parsed.grounding) {
            grounding = parsed.grounding;
          }

          // ── skill (tool trace start) ──
          if (parsed.skill) {
            // Remove empty thinking element if no thinking content
            if (!thinkingText) {
              store().appendThinkingToken(''); // signal no thinking
            }
            skillsUsed.push(parsed.skill);
            const trace: SkillTrace = { name: parsed.skill, input: {}, output: '' };
            skillTraces.push(trace);
            // Show trace in real-time during streaming
            store().addStreamToolUse({
              toolId: parsed.skill,
              toolName: parsed.skill,
              input: {},
              status: 'running',
            });
          }

          // ── skill_input ──
          if (parsed.skill_input && parsed.input) {
            const lastTrace = skillTraces[skillTraces.length - 1];
            if (lastTrace) {
              lastTrace.input = parsed.input;
              store().updateStreamToolUse(
                lastTrace.name,
                { input: parsed.input as Record<string, unknown> },
              );
            }
          }

          // ── skill_result (tool complete) ──
          if (parsed.skill_result) {
            const toolName = parsed.skill_result;
            const toolOutput = (parsed.output as string) ?? '';
            const isError = typeof toolOutput === 'string' && /^(Error|error)\b/.test(toolOutput);
            store().setLastTool(toolName, toolOutput, isError);

            // Match the FIRST not-yet-resolved trace with this name — tools
            // complete in order, so this assigns each skill_result to its own
            // call. Matching purely by name overwrote the first trace's output
            // for every repeated same-named call (e.g. three task_create),
            // leaving the rest empty.
            const previewImage = parsed.preview_image;

            const trace = skillTraces.find(t => t.name === parsed.skill_result && !t.resolved);
            if (trace) {
              trace.output = toolOutput;
              trace.resolved = true;
              if (previewImage) trace.previewImage = previewImage;
            }

            // Update real-time trace to complete
            store().updateStreamToolUse(toolName, {
              status: isError ? 'error' : 'complete',
              result: toolOutput,
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
            if (!isError && (
              toolName === 'ws_create_file' ||
              toolName === 'ws_write_file' ||
              toolName === 'ws_delete' ||
              toolName === 'ws_rename' ||
              toolName === 'ws_move' ||
              toolName === 'ws_mkdir' ||
              toolName === 'ws_copy' ||
              toolName === 'ws_duplicate'
            )) {
              window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
            }
          }

          renderEventCards(parsed, store, sessionId);
          // ── ws_auto_applied (pre-approved action executed server-side) ──
          if (parsed.ws_auto_applied) {
            // Refresh workspace tree
            window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
            // Mark file as dirty in editor if open
            const autoApplied = parsed.ws_auto_applied;
            if (autoApplied.path) {
              const ws = useWorkspaceStore.getState();
              const tab = ws.editorTabs.find(t => t.path === autoApplied.path);
              if (tab) {
                ws.markDirty(autoApplied.path, autoApplied.content ?? tab.content);
              }
            }
          }

          // ── ws_workspace_prompt ──
          if (parsed.ws_workspace_prompt) {
            hasPendingApprovals = true;
            store().addMessage({
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString(),
              toolUse: [{
                toolId: 'ws_workspace_prompt',
                toolName: 'ws_workspace_prompt',
                input: parsed.ws_workspace_prompt,
                status: 'pending',
              }],
            });
          }

          // ── approval_request (generic, declarative) ──
          if (parsed.approval_request) {
            hasPendingApprovals = true;
            const req = parsed.approval_request;

            // Backend doesn't emit skill_result for tools that pause for
            // approval; mark the last running trace complete so the UI
            // doesn't show a spinning skill forever.
            const runningTraces = store().currentStreamToolUse;
            const lastRunning = [...runningTraces].reverse().find(t => t.status === 'running');
            if (lastRunning) {
              store().updateStreamToolUse(lastRunning.toolName, {
                status: 'complete',
                result: `Awaiting approval: ${req.summary}`,
              });
              const localTrace = skillTraces.find(t => t.name === lastRunning.toolName);
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
              sessionId,
            };

            const st = store();

            // Session-memory routing: category from the spec, not action.
            const catMode = st.getSessionApproval(req.category);
            if (catMode === 'allow') {
              void (async () => {
                try {
                  const outcome = await executeApproval({
                    action: approval.action,
                    payload: approval.payload,
                  });
                  if (outcome.ok) {
                    // git_clone (and any future workspace-connecting action)
                    // reports the opened path here — switch to it so the panel
                    // opens, mirroring the ws_folder_opened SSE handler below.
                    if (outcome.ws_folder_opened) {
                      useUIStore.getState().setWsConnected(true, outcome.ws_folder_opened);
                    }
                    window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
                  }
                  await sendApprovalContinuation(approval, sessionId, true, signal, outcome);
                } catch (err) {
                  // executeApproval throws ApiError on any non-2xx / network
                  // failure. Without this guard the IIFE rejected unhandled:
                  // the continuation never sent and the turn hung silently.
                  // Tell the model the truth — a FAILED tool_result (accepted
                  // but ok:false) — so it can react instead of waiting forever,
                  // and surface the failure to the user.
                  const detail = toError(err).message;
                  console.error('Auto-approved action failed:', err);
                  useUIStore.getState().addToast({
                    type: 'error',
                    message: `Auto-approved "${approval.action}" failed: ${detail}`,
                    priority: TOAST_PRIORITY.high,
                  });
                  await sendApprovalContinuation(approval, sessionId, true, signal, {
                    ok: false,
                    error: String(err),
                  });
                }
              })();
            } else if (catMode === 'deny') {
              void (async () => {
                // Same guard as the allow path: a rejected continuation here
                // (e.g. the continuation fetch throws before its own try) would
                // otherwise surface as an unhandled rejection and strand the turn.
                try {
                  await sendApprovalContinuation(approval, sessionId, false, signal);
                } catch (err) {
                  console.error('Auto-denied continuation failed:', err);
                  useUIStore.getState().addToast({
                    type: 'error',
                    message: `Failed to record denial for "${approval.action}": ${toError(err).message}`,
                    priority: TOAST_PRIORITY.high,
                  });
                }
              })();
            } else {
              st.enqueueApproval(approval);
            }
          }

          // ── user_question (ask_user tool) ──
          // The assistant can fire multiple ask_user_question tool calls in
          // one streaming round (parallel tool_use). Collapse them into a
          // single assistant message carrying a `userQuestions` list so the
          // renderer can show a tabbed multi-question card with one batched
          // submit at the end. Single-question rounds still populate the
          // legacy `userQuestion` field for back-compat.
          if (parsed.user_question) {
            hasUserQuestion = true;
            const uq = parsed.user_question as { question?: string; options?: string[]; tool_use_id?: string };
            const entry = {
              question: uq.question ?? '',
              options: uq.options ?? [],
              toolUseId: uq.tool_use_id ?? '',
            };

            const st = store();
            const msgs = st.messages;
            const lastIdx = msgs.length - 1;
            const last = lastIdx >= 0 ? msgs[lastIdx] : null;
            // If the previous message in this same streaming round already
            // carries a question group, append to it instead of adding a
            // new message. We detect "same round" by `_inFlight` — set when
            // we add the first question of the round and cleared when the
            // stream ends.
            if (last && last._inFlight && (last.userQuestions || last.userQuestion)) {
              const existing = last.userQuestions ?? (last.userQuestion ? [last.userQuestion] : []);
              const updated: ChatMessage = {
                ...last,
                userQuestion: undefined,
                userQuestions: [...existing, entry],
              };
              const next = msgs.slice(0, lastIdx).concat(updated);
              st.setMessages(next);
            } else {
              // Surface only NON-ask_user_question traces above the tabbed
              // card. The tabs themselves already represent every
              // ask_user_question call in this round (Q 1/N, Q 2/N, …),
              // so showing duplicate "🔧 ask_user_question" chips next to
              // them is noisy and unprofessional. Other prior tools — a
              // web_fetch that the model ran before asking, for example —
              // still render normally.
              const toolUseEntries: ToolUseEvent[] = skillTraces
                .filter((t) => t.name !== 'ask_user_question')
                .map((t) => ({
                  toolId: t.name,
                  toolName: t.name,
                  input: t.input ?? {},
                  result: t.output || undefined,
                  status: 'complete' as const,
                  previewImage: t.previewImage,
                }));
              st.addMessage({
                role: 'assistant',
                // Fold any prose the model streamed before asking (e.g.
                // "Great question! Let me ask what fits you best.") into THIS
                // message so it renders above the question card, in order.
                // Without this the prose was committed as a separate message
                // at stream-end and landed *below* the card.
                content: fullResponse,
                timestamp: new Date().toISOString(),
                userQuestions: [entry],
                toolUse: toolUseEntries.length > 0 ? toolUseEntries : undefined,
                // Team activity from before the question stays with its
                // traces on this message.
                teamReports: st.takeTeamReports(),
                _thinkingMs: thinkingMs > 0 ? Math.round(thinkingMs) : undefined,
                _thinkingText: thinkingText || undefined,
                _inFlight: true,
              });
              // Prose is now owned by the question message — clear the
              // accumulator so finishStream doesn't re-commit it as a second,
              // out-of-order assistant message.
              fullResponse = '';
              // Drain — these traces are now visible on the question card.
              // Without this, finishStream would attach them to a second
              // assistant message and render the same tool chips twice.
              skillTraces.length = 0;
            }
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

          // ── tool_pool (progressive disclosure telemetry) ──
          if (parsed.tool_pool) {
            useUIStore.getState().setToolPoolStats(
              parsed.tool_pool as import('@/stores/uiStore').ToolPoolStats,
            );
          }

          // ── notify_user ──
          if (parsed.notify_user) {
            const n = parsed.notify_user as Record<string, string>;
            const msg = n.message ?? '';
            // Honor the tool's declared status (the schema always offered
            // success/warning/error; the toast previously flattened them all
            // to info) and the title; warnings/errors linger longer.
            const type =
              n.status === 'success' || n.status === 'warning' || n.status === 'error'
                ? n.status
                : ('info' as const);
            const duration = type === 'warning' || type === 'error' ? 9000 : 5000;
            useUIStore.getState().addToast({ type, title: n.title || undefined, message: msg, duration });
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

          // ── team_results ──
          // The final structured payload from execute_team_create. Fold it
          // into the same turn-local report; it reaches the conversation on
          // the committed assistant message alongside the team_create trace,
          // where TeamReportCard renders it. (The old fallback that created a
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
            st.addMessage({
              role: 'cron_event',
              content: '',
              timestamp: payload.timestamp ?? new Date().toISOString(),
              cronEvent: payload,
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

          // ── thinking_start ──
          if (parsed.thinking_start) {
            store().setThinkingStart();
          }

          // ── thinking (content) ──
          if (parsed.thinking) {
            thinkingText += parsed.thinking;
            store().appendThinkingToken(parsed.thinking);
          }

          // ── thinking_stop ──
          if (parsed.thinking_stop) {
            thinkingMs = thinkingBlockStart ? performance.now() - thinkingBlockStart : 0;
            store().setThinkingStop();
          }

          // ── usage ──
          // The backend (server/chat/routes.py) sends BOTH per-round counts
          // (`input_tokens`/`output_tokens`) and cumulative running totals
          // (`total_input`/`total_output`) on every usage frame, and its
          // `estimated_cost_usd` is cumulative too. Prefer the totals so the
          // counter reflects the whole turn instead of collapsing to the last
          // round; fall back to per-round for any emitter that omits totals.
          if (parsed.usage) {
            inputTokens = parsed.usage.total_input ?? parsed.usage.input_tokens ?? inputTokens;
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
              thinkingMs = thinkingBlockStart ? performance.now() - thinkingBlockStart : thinkingMs;
              store().setThinkingElapsed(thinkingMs);
            }
            fullResponse += parsed.text;
            store().appendStreamToken(parsed.text);
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
            store().appendStreamToken(errLine);
          }

        } catch (e) {
          console.warn('SSE parse error:', toError(e).message, 'line:', data);
        }
      }
    }
  } finally {
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
    grounding,
    pendingArtifact,
    pendingPlan,
  };
}

/** Outcome of actually executing the approved (or denied) action on the
 *  backend. Pass this into `sendApprovalContinuation` so Bedrock receives a
 *  truthful tool_result instead of a hard-coded "succeeded" string — the
 *  pre-existing message lied to the model whenever a write actually failed
 *  (or, in the worst case I just shipped, when the write was never even
 *  attempted because no one called the workspace endpoint).
 */
export interface ApprovalOutcome {
  ok: boolean;
  /** Free-form output to forward to the model (stdout, error message…). */
  output?: string;
  /** Concise error description; rendered if ok is false. */
  error?: string;
}

/**
 * Send an approval continuation (accept or deny) through the full SSE pipeline.
 */
export async function sendApprovalContinuation(
  approval: PendingApproval,
  sessionId: string,
  accepted: boolean,
  signal?: AbortSignal,
  outcome?: ApprovalOutcome,
): Promise<void> {
  // Parallel sessions: bind the OWNING session's store from the sessionId
  // this stream was started with. Never resolve the active session here —
  // every store() call below must keep landing in the same session no
  // matter what the user is viewing.
  const store = () => getChatStore(sessionId).getState();
  store().setStreaming(true);

  const target = (approval.payload?.path as string | undefined)
    ?? (approval.payload?.command as string | undefined)
    ?? approval.summary
    ?? 'N/A';
  let content: string;
  if (!accepted) {
    content = `[User denied] ${approval.action}: ${target}. The user rejected this change.`;
  } else if (outcome) {
    if (outcome.ok) {
      const detail = outcome.output ? `\n\n${outcome.output}` : '';
      content = `[User approved] ${approval.action}: ${target}. The filesystem operation succeeded.${detail}`;
    } else {
      const detail = outcome.error ?? outcome.output ?? 'unknown error';
      content = `[User approved but the operation FAILED] ${approval.action}: ${target}. Error: ${detail}`;
    }
  } else {
    // Back-compat: caller did not execute the action client-side. We still
    // mark it as approved (legacy behaviour) but the model is going to
    // believe the write happened even if no one wrote anything. Newer call
    // sites should always pass an outcome.
    content = `[User approved] ${approval.action}: ${target}.`;
  }

  // ws_edit_file sets this flag when the old_string matched only after quote
  // normalization. Tell the model its match was inexact so it doesn't assume
  // byte-for-byte file state on the next edit. The flag rides only on edit
  // payloads, so this appends nothing for writes/commands/etc.
  if (accepted && approval.payload?.matched_via_normalization === true) {
    content +=
      ' Note: old_string did not match exactly; it matched only after quote'
      + ' normalization (the file uses typographic quotes that differ from the'
      + ' text sent), and the original quote characters were preserved.';
  }

  const body: Record<string, unknown> = {
    question: '',
    session_id: sessionId,
    // Carry the selected model so the continuation resumes on the SAME backend
    // it paused on. Without this the endpoint falls back to the default cloud
    // model, which would strand a paused local (Gemma) tool turn.
    model: useSettingsStore.getState().selectedModel,
    approved_tool_result: {
      tool_use_id: approval.toolUseId,
      content,
      accepted,
    },
    session_approvals: store().sessionApprovals,
    session_denials: store().sessionDenials,
  };

  // MCP servers are resolved by the backend from each server's persisted
  // `enabled` flag, so the continuation needs no per-request override.

  // ALWAYS register a controller for this continuation leg so Stop/ESC can
  // reach it — even for an auto-approved ("Yes, all X") continuation that runs
  // after the outer stream has already ended and released its own controller.
  // Reusing only the outer signal left that case unstoppable: the registry no
  // longer held the outer controller, and we registered nothing. When an outer
  // signal is passed, chain it so a cancel there still aborts this leg.
  const ownController = new AbortController();
  registerStreamController(sessionId, ownController);
  if (signal) {
    if (signal.aborted) ownController.abort();
    else signal.addEventListener('abort', () => ownController.abort(), { once: true });
  }
  const continuationSignal = ownController.signal;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: continuationSignal,
    });

    if (!response.ok || !response.body) throw new Error('Continuation failed');

    const result = await readSSEStream(response, sessionId, continuationSignal);

    const contTeamReports = store().takeTeamReports();
    if (result.fullResponse || result.pendingArtifact || result.pendingPlan || contTeamReports) {
      const contToolUse: ToolUseEvent[] = result.skillTraces.map(t => ({
        toolId: t.name,
        toolName: t.name,
        input: t.input ?? {},
        result: t.output || undefined,
        status: 'complete' as const,
        previewImage: t.previewImage,
      }));
      store().addMessage({
        role: 'assistant',
        content: result.fullResponse,
        timestamp: new Date().toISOString(),
        skills: result.skillsUsed.length > 0 ? result.skillsUsed : undefined,
        traces: result.skillTraces.length > 0 ? result.skillTraces : undefined,
        toolUse: contToolUse.length > 0 ? contToolUse : undefined,
        teamReports: contTeamReports,
        // The continuation can also yield a create_artifact artifact; pin it
        // to this message so the artifact card and the model's text live
        // together in one card and tool chips render only once.
        programArtifact: result.pendingArtifact ?? undefined,
        plan: result.pendingPlan ?? undefined,
        _thinkingMs: result.thinkingMs > 0 ? Math.round(result.thinkingMs) : undefined,
        _thinkingText: result.thinkingText || undefined,
        _usage: (result.inputTokens > 0 || result.outputTokens > 0)
          ? { input_tokens: result.inputTokens, output_tokens: result.outputTokens }
          : undefined,
        grounding: result.grounding,
      });
    }

    // Refresh workspace tree after approval
    window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
    window.dispatchEvent(new CustomEvent('whisper-git-refresh'));
  } catch (err) {
    if (toError(err).name === 'AbortError') {
      // User stop: the kill switch already finalized the UI — a fabricated
      // "failed to continue" bubble here would misreport a deliberate stop.
    } else {
      console.error('Approval continuation failed:', err);
      store().addMessage({
        role: 'assistant',
        content: '*Error: Failed to continue after approval.*',
        timestamp: new Date().toISOString(),
      });
    }
  } finally {
    if (ownController) releaseStreamController(sessionId, ownController);
    // Only clear streaming if no more approvals pending
    if (!store().currentApproval && store().approvalQueue.length === 0) {
      store().setStreaming(false);
    }
  }
}
