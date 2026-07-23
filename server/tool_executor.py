"""
Streaming tool executor — lifecycle management, batching, and result processing.

Responsibilities:
  - Per-tool state tracking (queued → executing → completed | skipped)
  - Input mutation safety (three-copy pattern)
  - Batch partitioning for concurrent execution
  - Hook lifecycle (PreToolUse / PostToolUse / PostToolUseFailure)
  - Sibling abort on command errors
  - Permission and denial checks
  - Result post-processing (approvals, budgeting, SSE formatting)

This module has NO knowledge of individual tool handlers — that's tool_router.py.
"""

import asyncio
import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from server.auto_mode import classify_tool_call
from server.hooks import run_hooks
from server.security.explainer import explain_permission
from server.security.permissions import MODE_BYPASS, resolve_static_decision
from server.tool_router import SIDE_EFFECT_PAUSE, route_tool
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

# Tools whose errors cascade to subsequent writes
_COMMAND_TOOLS = {"ws_run_command", "run_python"}

# Write tools blocked in plan mode
_PLAN_MODE_BLOCKED = {
    "ws_write_file",
    "ws_create_file",
    "ws_edit_file",
    "ws_delete_file",
    "ws_run_command",
    "ws_merge_worktree",
}

# Max denials before auto-blocking
MAX_AUTO_DENIALS = 2


def _append_hook_context(state: "ToolState", outcome) -> None:
    """Fold a PostToolUse[Failure] hook's additionalContext into the tool result
    so the model actually reads it next turn (the old fire_hook discarded it)."""
    if not outcome.contexts:
        return
    note = "\n".join(f"[Hook] {c}" for c in outcome.contexts)
    state.output = f"{state.output}\n\n{note}" if state.output else note


# ---------------------------------------------------------------------------
# ToolState — per-tool tracking
# ---------------------------------------------------------------------------


@dataclass
class ToolState:
    """Tracks the lifecycle of a single tool execution.

    Status transitions:
      queued → executing → completed → yielded
                        → skipped (pre-execution checks failed)
    The 'yielded' status is set after the result has been emitted
    to the SSE stream, closing the full lifecycle.
    """

    tool_use: dict  # Original tool_use block from Bedrock
    status: str = "queued"  # queued | executing | completed | skipped | yielded
    output: str = ""
    side_effects: list[dict] = field(default_factory=list)

    @property
    def tool_id(self) -> str:
        return self.tool_use["id"]

    @property
    def tool_name(self) -> str:
        return self.tool_use["name"]


# ---------------------------------------------------------------------------
# Batch partitioning
# ---------------------------------------------------------------------------


def partition_batches(
    tool_uses: list[dict],
    is_concurrent_safe: callable,
) -> list[list[dict]]:
    """Split tool_uses into consecutive batches for execution.

    Consecutive concurrent-safe tools form a single parallel batch.
    Each non-safe tool becomes its own batch (executed serially).

    Example:
        [read, read, write, read, write] → [[read, read], [write], [read], [write]]
    """
    if not tool_uses:
        return []

    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_is_safe = None

    for tu in tool_uses:
        safe = is_concurrent_safe(tu["name"])
        if safe and current_is_safe is True:
            # Continue the parallel batch
            current_batch.append(tu)
        else:
            # Flush previous batch and start a new one
            if current_batch:
                batches.append(current_batch)
            current_batch = [tu]
            current_is_safe = safe

    if current_batch:
        batches.append(current_batch)

    return batches


# ---------------------------------------------------------------------------
# Tool execution with lifecycle
# ---------------------------------------------------------------------------


async def execute_tool_batch(
    tool_uses: list[dict],
    *,
    is_concurrent_safe: callable,
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
    transcript: str,
    attachments: dict | None,
    session_id: str,
    session_denials: dict,
    model_id: str,
    plan_mode: bool,
    mode: str = "default",
    effort_label: str | None = None,
) -> list[ToolState]:
    """Execute a batch of tool_use blocks with full lifecycle management.

    Returns ToolState objects in the same order as the input tool_uses.
    """
    # Build state objects indexed by tool_use_id for quick lookup
    states = [ToolState(tool_use=tu) for tu in tool_uses]
    state_by_id = {s.tool_id: s for s in states}

    # Shared abort flag — set when a command tool fails
    command_error: str | None = None

    async def _run_one(state: ToolState) -> None:
        """Execute a single tool with lifecycle guards. Mutates state in place."""
        nonlocal command_error
        tool_name = state.tool_name
        state.status = "executing"

        # Input mutation safety — three separate copies:
        #   1. state.tool_use["input"] — original (preserved for API/Bedrock messages)
        #   2. hook_input — clone for hooks (observable, never mutated by execution)
        #   3. call_input — clone for execution (may be mutated by tool handlers)
        hook_input = copy.deepcopy(state.tool_use["input"])
        call_input = copy.deepcopy(state.tool_use["input"])
        call_input["__session_id__"] = session_id

        # --- Pre-execution checks ---

        # Sibling abort: skip writes if a command tool failed
        if command_error and not is_concurrent_safe(tool_name):
            state.status = "skipped"
            state.output = f"[Skipped] Prior command failed: {command_error}"
            return

        # Plan mode block — emit SSE so frontend can offer the upgrade dialog
        if plan_mode and tool_name in _PLAN_MODE_BLOCKED:
            state.status = "skipped"
            state.output = f"[Plan Mode] Tool '{tool_name}' is blocked in plan mode."
            state.side_effects = [
                {
                    "plan_blocked": {
                        "tool_name": tool_name,
                        "tool_input": {
                            k: v
                            for k, v in state.tool_use.get("input", {}).items()
                            if k != "__session_id__"
                        },
                        "message": f"Plan mode is active. '{tool_name}' was blocked.",
                    }
                }
            ]
            return

        # Denial tracking (bypassPermissions is "no prompts, no blocks" — a stale
        # denial count from an earlier mode must not hold under it)
        if mode != MODE_BYPASS:
            denials = session_denials.get(tool_name, 0)
            if denials >= MAX_AUTO_DENIALS:
                state.status = "skipped"
                state.output = f"[Denied] '{tool_name}' has been denied {denials} times. Re-enable in permissions settings to use again."
                return

        # --- Hook: PreToolUse (in-process plugins + shell hooks; can block/rewrite) ---
        pre = await run_hooks(
            "PreToolUse",
            {
                "event": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": hook_input,
                "session_id": session_id,
                "model_id": model_id,
            },
            tool_name=tool_name,
        )
        if pre.blocked:
            state.status = "skipped"
            state.output = f"[Hook denied] {pre.reason}"
            # Plugin security denials carry findings → existing security_blocked UI.
            # Plain shell/project denials get the generic hook_blocked frame.
            if pre.findings is not None:
                state.side_effects.append(
                    {"security_blocked": {"reason": pre.reason, "findings": pre.findings}}
                )
            else:
                state.side_effects.append(
                    {"hook_blocked": {"tool_name": tool_name, "reason": pre.reason}}
                )
            return
        # A hook may rewrite the tool input before execution.
        if pre.updated_input is not None:
            call_input = copy.deepcopy(pre.updated_input)
            call_input["__session_id__"] = session_id

        # --- Dispatch ---
        try:
            output, side_effects = await route_tool(
                tool_name,
                call_input,
                loop=loop,
                executor=executor,
                transcript=transcript,
                attachments=attachments,
                session_id=session_id,
                model_id=model_id,
                tool_use_id=state.tool_id,
                effort_label=effort_label,
            )
            state.output = output
            state.side_effects = side_effects
            state.status = "completed"

            # Hook: PostToolUse — additionalContext is fed back to the model by
            # appending it to the tool result the model reads next turn.
            post = await run_hooks(
                "PostToolUse",
                {
                    "event": "PostToolUse",
                    "tool_name": tool_name,
                    "tool_input": hook_input,
                    "tool_output": str(output)[:2000],
                    "session_id": session_id,
                    "model_id": model_id,
                },
                tool_name=tool_name,
                allow_rewrite=False,
            )
            _append_hook_context(state, post)

        except Exception as e:
            log.error("Tool execution error (%s): %s", tool_name, e, exc_info=True)
            state.output = f"[Tool Error] {e}"
            state.status = "completed"

            fail = await run_hooks(
                "PostToolUseFailure",
                {
                    "event": "PostToolUseFailure",
                    "tool_name": tool_name,
                    "tool_input": hook_input,
                    "tool_output": str(e),
                    "session_id": session_id,
                    "model_id": model_id,
                },
                tool_name=tool_name,
                allow_rewrite=False,
            )
            _append_hook_context(state, fail)

            # Command errors cascade to subsequent writes
            if tool_name in _COMMAND_TOOLS:
                command_error = f"{tool_name}: {e}"

    # --- Execute batches ---
    batches = partition_batches(tool_uses, is_concurrent_safe)
    for batch in batches:
        batch_states = [state_by_id[tu["id"]] for tu in batch]
        if len(batch_states) == 1:
            await _run_one(batch_states[0])
        else:
            await asyncio.gather(*[_run_one(s) for s in batch_states])

    return states


# ---------------------------------------------------------------------------
# Result post-processing
# ---------------------------------------------------------------------------


async def _execute_ws_approval_inline(ws_parsed: dict, *, agent: bool = False) -> str:
    """Run a pre-approved action through the same ApprovalSpec executor the
    UI's Yes button uses. Single execution path — no duplicated write/delete/
    command logic. Adding a new approval-required tool requires only a
    `register()` call, not a branch here.

    `agent=True` marks the call as originating from a subagent's UNCONDITIONAL
    auto-approve path (server/agents/runtime.py) — no human, no category gate.
    It stamps `__agent__` onto the payload so high-blast-radius executors (e.g.
    GitHub mutations) can refuse to run unattended. The chat/auto-mode caller
    leaves this False, because there a real user or the auto-mode classifier
    authorised the action."""
    from server.approval import registry as approval_registry

    action = ws_parsed.get("action", "write")
    spec = approval_registry.get(action)
    if not spec:
        return f"Error: unknown action '{action}' (no ApprovalSpec registered)"
    payload = spec.build_payload(ws_parsed)
    # Forward session_id if present in the original parsed payload so
    # executors that need it (worktree enter/exit) can read it.
    if "session_id" in ws_parsed and "session_id" not in payload:
        payload["session_id"] = ws_parsed["session_id"]
    # Stamp the subagent origin AFTER build_payload (which whitelists fields when
    # payload_fields is set and would otherwise drop it).
    if agent:
        payload["__agent__"] = True
    try:
        outcome = await spec.executor(payload)
    except Exception as e:  # noqa: BLE001
        return f"Error executing {action}: {e}"
    if outcome.ok:
        return outcome.output or f"[OK] {action}"
    return f"Error: {outcome.error or 'unknown error'}"


async def process_tool_results(
    states: list[ToolState],
    budget_fn: callable,
    *,
    session_approvals: dict | None = None,
    config: dict | None = None,
    model_id: str = "",
    recent_messages: list[dict] | None = None,
    mode: str = "default",
) -> tuple[list[dict], list[str], bool, bool]:
    """Post-process completed tool states into Bedrock messages and SSE events.

    Args:
        states: Completed ToolState objects in request order.
        budget_fn: Function (tool_name, output) -> budgeted_output for large results.
        session_approvals: Category-level pre-approvals from frontend (e.g. {"write": "allow"}).

    Returns:
        (tool_results, sse_events, has_pending_approval, has_user_question)
        - tool_results: List of tool_result dicts for Bedrock messages
        - sse_events: List of SSE event strings (already ndjson-encoded)
        - has_pending_approval: True if any tool needs user approval before the LLM continues
        - has_user_question: True if any tool triggered a pause
    """
    if session_approvals is None:
        session_approvals = {}

    if session_approvals:
        log.info("process_tool_results: session_approvals=%s", session_approvals)

    tool_results: list[dict] = []
    sse_events: list[str] = []
    has_pending_approval = False
    has_user_question = False

    for state in states:
        # Flush side effects as SSE events
        for effect in state.side_effects:
            if SIDE_EFFECT_PAUSE in effect:
                has_user_question = True
                continue
            sse_events.append(ndjson_dumps(effect))

        tool_output = state.output

        # Workspace-prompt detection: a write-type tool fired without a
        # workspace (or against a path outside the current one). Show a
        # folder picker to the user; stream resumes on continuation turn.
        if isinstance(tool_output, str) and tool_output.startswith("[WS_WORKSPACE_PROMPT]"):
            prompt_data = tool_output[len("[WS_WORKSPACE_PROMPT]") :]
            try:
                prompt_parsed = json.loads(prompt_data)
            except json.JSONDecodeError as e:
                log.error("Malformed [WS_WORKSPACE_PROMPT] payload: %s", e)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": f"[Error] Malformed workspace-prompt payload: {e}",
                    }
                )
                continue
            prompt_parsed = {**prompt_parsed, "tool_use_id": state.tool_id}
            sse_events.append(ndjson_dumps({"ws_workspace_prompt": prompt_parsed}))
            has_pending_approval = True
            break

        # Workspace approval detection. Tools signal "needs approval" by
        # returning a string with the [WS_APPROVAL] prefix containing JSON.
        # The prefix is purely internal — the frontend only sees the new
        # generic `approval_request` event we emit below, shaped from the
        # ApprovalSpec registered for the action.
        if isinstance(tool_output, str) and tool_output.startswith("[WS_APPROVAL]"):
            from server.approval import registry as approval_registry

            ws_data = tool_output[len("[WS_APPROVAL]") :]
            try:
                ws_parsed = json.loads(ws_data)
            except json.JSONDecodeError as e:
                log.error("Malformed [WS_APPROVAL] payload: %s", e)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": f"[Error] Malformed approval payload: {e}",
                    }
                )
                continue
            action = ws_parsed.get("action", "")
            spec = approval_registry.get(action)
            if not spec:
                log.error("approval: no ApprovalSpec registered for action=%r — refusing", action)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": (
                            f"[Error] No approval registered for action {action!r}. "
                            f"Refusing to run. Register an ApprovalSpec in server/approval/bootstrap.py."
                        ),
                    }
                )
                continue
            category = spec.category

            log.info(
                "approval: action=%s category=%s session_approvals=%s",
                action,
                category,
                session_approvals,
            )

            # Trusted folder skill: auto-approve running its OWN bundled scripts
            # (command path resolves inside a trusted skill's directory). Scoped
            # to the skill's own files; validate_command still applied upstream.
            auto_allow_trusted = False
            if action == "terminal_run":
                from server.skills import command_runs_trusted_skill

                if command_runs_trusted_skill(ws_parsed.get("command", "")):
                    auto_allow_trusted = True

            # Precedence: bypassPermissions → trusted skill → session approvals →
            # custom rules → dontAsk → acceptEdits → auto (classifier) → ask.
            # See server.security.permissions.resolve_static_decision.
            decision = resolve_static_decision(
                state.tool_name,
                ws_parsed,
                category,
                session_approvals,
                mode,
                auto_allow_trusted,
            )
            if decision is None:
                # mode == "auto" and nothing else resolved it — only invoke the
                # classifier for actions that actually need approval (read-only
                # tools never reach this point). model_id is empty for the
                # offline local-model path, which must stay Bedrock-free.
                cfg = config or {}
                if model_id and cfg.get("auto_mode_enabled"):
                    verdict = await classify_tool_call(state.tool_name, ws_parsed, cfg, model_id)
                    decision = "allow" if verdict.get("decision") == "allow" else "ask"
                else:
                    decision = "ask"

            if decision == "allow":
                # Pre-approved: execute inline and return result to LLM
                log.info("approval: executing inline (pre-approved)")
                from server.workspace import get_workspace_path

                ws_before = get_workspace_path()
                result = await _execute_ws_approval_inline(ws_parsed)
                # Auto-applied event keeps the file tree / editor in sync.
                sse_events.append(ndjson_dumps({"ws_auto_applied": ws_parsed}))
                # If the action switched the workspace (e.g. git_clone with
                # open=true), tell the frontend to open the panel. Detected by
                # diffing the connected path — the same generic signal the
                # manual approval route uses, so no per-action branch here.
                ws_after = get_workspace_path()
                if ws_after and ws_after != ws_before:
                    sse_events.append(ndjson_dumps({"ws_folder_opened": ws_after}))
                tool_output = result
                # Fall through to normal result processing below
            elif decision == "deny":
                path = ws_parsed.get("path", ws_parsed.get("command", ""))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": f"[Denied by session rule] {action}: {path} — blocked by user.",
                    }
                )
                continue
            else:
                # Fetch risk explanation (3s timeout inside explain_permission)
                explanation = None
                if config and model_id:
                    explanation = await explain_permission(
                        tool_name=state.tool_name,
                        tool_input=ws_parsed,
                        recent_messages=recent_messages or [],
                        config=config,
                        model_id=model_id,
                    )

                # Build the generic approval_request event from the spec.
                # No more per-action shape — the frontend reads `preview`
                # and picks one of four renderers (diff/command/list/text).
                payload = spec.build_payload(ws_parsed)
                summary = spec.render_summary(ws_parsed)
                preview = spec.preview
                risk_hint = spec.risk_hint

                event = {
                    "approval_request": {
                        "tool_use_id": state.tool_id,
                        "action": action,
                        "category": category,
                        "preview": preview,
                        "summary": summary,
                        "payload": payload,
                        "risk_hint": risk_hint,
                        "explanation": explanation,
                    }
                }
                sse_events.append(ndjson_dumps(event))

                # Pause the stream. All approval actions (command, write,
                # create, delete) wait for the user. After approval, the
                # frontend sends a new /api/chat turn carrying the real
                # tool_result so the LLM resumes with truthful state.
                has_pending_approval = True
                break  # Do not execute sibling tools in this batch.

        # Preview screenshot detection: preview_screenshot's executor returns
        # a sentinel carrying base64 JPEG bytes + a caption. Unlike every
        # other tool (whose content is a plain string), this ONE tool_result
        # must be a list of Anthropic content blocks — an image block plus a
        # sibling text block — so the model actually sees the pixels, not a
        # wall of base64 text. Matches the shape already used for
        # user-uploaded image attachments (server/chat/routes.py). Placed
        # before budget_fn below: truncating a base64 payload would corrupt
        # the image.
        if isinstance(tool_output, str) and tool_output.startswith("[WS_PREVIEW_IMAGE]"):
            img_data = tool_output[len("[WS_PREVIEW_IMAGE]") :]
            try:
                img_parsed = json.loads(img_data)
            except json.JSONDecodeError as e:
                log.error("Malformed [WS_PREVIEW_IMAGE] payload: %s", e)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": f"[Error] Malformed preview-image payload: {e}",
                    }
                )
                continue
            caption = img_parsed.get("caption", "Screenshot")
            media_type = img_parsed.get("media_type", "image/jpeg")
            data = img_parsed.get("data", "")
            sse_events.append(
                ndjson_dumps(
                    {
                        "skill_result": state.tool_name,
                        "output": caption,
                        # Frontend-only field — PreviewScreenshotCard keys off this
                        # to render the <img>; other skill_result consumers ignore it.
                        "preview_image": {"media_type": media_type, "data": data},
                    }
                )
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": state.tool_id,
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": data},
                        },
                        {"type": "text", "text": caption},
                    ],
                }
            )
            state.status = "yielded"
            continue

        # Budget large results — but NOT for tools whose result is a model
        # prompt (prompt/folder skills, and content executors like
        # summarize_transcript / analyze_document). Their instructions sit at
        # the payload tail, so head-truncation would silently strip them and
        # leave the model unable to act on the remaining transcript. See
        # server.skills.produces_model_prompt and server.chat.budget.
        if isinstance(tool_output, str):
            from server.skills import produces_model_prompt

            if not produces_model_prompt(state.tool_name):
                tool_output = budget_fn(state.tool_name, tool_output)

        # Emit result preview as SSE
        if not isinstance(tool_output, str):
            tool_output = str(tool_output)
        preview = tool_output[:2000] + ("..." if len(tool_output) > 2000 else "")
        sse_events.append(ndjson_dumps({"skill_result": state.tool_name, "output": preview}))

        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": state.tool_id,
                "content": tool_output,
            }
        )

        # Mark as yielded — result has been emitted to the stream
        state.status = "yielded"

    # Bedrock requires a tool_result for every tool_use in the preceding
    # assistant message. When we break early on pending approval OR when
    # ask_user_question pauses the stream, any remaining states must still
    # get a placeholder result so the next request's messages array stays
    # well-formed. The placeholder is later overwritten with the real
    # answer / approval outcome on the continuation turn.
    if has_pending_approval:
        from server.skills import produces_model_prompt

        emitted_ids = {r["tool_use_id"] for r in tool_results}
        not_executed_msg = (
            "[Not executed] A prior tool call in this turn is "
            "awaiting user approval. This tool call was canceled; "
            "re-issue it after the prior approval resolves."
        )
        for state in states:
            if state.tool_id in emitted_ids:
                continue
            out = state.output if isinstance(state.output, str) else str(state.output)
            # The tool that TRIGGERED the pause carries an internal approval
            # sentinel as its output and hasn't truly run — its real result
            # arrives on the continuation turn once the user approves. Keep the
            # not-executed placeholder for it.
            awaiting_approval = out.startswith(("[WS_APPROVAL]", "[WS_WORKSPACE_PROMPT]"))
            if state.status == "completed" and not awaiting_approval:
                # This sibling already executed with a real result before the
                # pause. Preserve its actual output so the model doesn't
                # re-issue work that already happened. (The resume path only
                # overwrites the approved tool's id, so a "[Not executed]"
                # placeholder here would never be corrected and would prompt a
                # duplicate call.)
                content = out
                if not produces_model_prompt(state.tool_name):
                    content = budget_fn(state.tool_name, content)
                preview = content[:2000] + ("..." if len(content) > 2000 else "")
                sse_events.append(
                    ndjson_dumps({"skill_result": state.tool_name, "output": preview})
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": content,
                    }
                )
            else:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": not_executed_msg,
                    }
                )
    elif has_user_question:
        # For ask_user_question: the tool's output is already "[PAUSE] …"
        # text appended to tool_results, but Bedrock hasn't seen the user's
        # actual answer yet. Replace each ask_user_question placeholder with
        # an explicit "awaiting answer" marker that the resume path will
        # rewrite when the user submits.
        ASK_PAUSE_MARKER = "[ASK_USER_PAUSE] Awaiting answer; will be replaced on continuation."
        emitted_ids = {r["tool_use_id"] for r in tool_results}
        for r in tool_results:
            content = r.get("content", "")
            if isinstance(content, str) and content.startswith("[PAUSE]"):
                r["content"] = ASK_PAUSE_MARKER
        for state in states:
            if state.tool_id in emitted_ids:
                continue
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": state.tool_id,
                    "content": ASK_PAUSE_MARKER,
                }
            )

    return tool_results, sse_events, has_pending_approval, has_user_question
