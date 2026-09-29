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

from server.approval.spec import WORKSPACE_ROOT_KEY, turn_workspace_refusal
from server.auto_mode import classify_tool_call
from server.hooks import run_hooks
from server.preview.start_policy import never_prompts
from server.security.explainer import explain_permission
from server.security.permissions import MODE_BYPASS, approval_floor, resolve_static_decision
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
    so the model actually reads it next turn."""
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
    # The connected workspace when the tool returned an approval sentinel:
    # the root its workspace-relative payload was resolved against. The gate
    # stamps it on the card (process_tool_results), so a workspace switch
    # before the gate runs cannot pair the payload with the new root.
    ws_root: str | None = None
    ws_root_taken: bool = False

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
    unattended: bool = False,
    guard_scope: str = "",
    event_channel: str | None = None,
    workspace_latch=None,
    tool_scope=None,
) -> list[ToolState]:
    """Execute a batch of tool_use blocks with full lifecycle management.

    ``tool_scope`` (server.agents.tool_access.ToolScope) is what an agent run
    may execute. A call to any name outside ``tool_scope.permitted`` is
    refused with a synthetic result before hooks, approvals or the router see
    it, whatever name the model emitted: the tools array only decides what
    the model is shown, and agents approve their own writes. None (every
    non-agent turn) leaves calls unscoped.

    ``workspace_latch`` (server.workspace.state.WorkspaceLatch) is the
    workspace the turn was assembled against. Every call in the batch runs
    under it: if the user disconnected that workspace, or switched away from
    it, since the turn began, workspace tools see no workspace and are refused
    at execution with the reason (the tool catalog itself never changes).
    None runs the batch unlatched.

    ``guard_scope`` keys the per-turn loop guard (server.chat.loop_guard): a
    call that repeats identical arguments after identical results, repeats a
    failing call unchanged, cycles, or exceeds a per-turn cap is refused with
    a synthetic result instead of dispatched. Empty (the default, and every
    direct test caller) disables the guard.

    ``unattended`` marks every dispatched call as originating from a turn with
    no human present (subagents today). It stamps ``__agent__`` onto the call
    input exactly like ``server.agents.runtime._with_session_id`` already does
    for the pre-migration agent loop, so high-blast-radius executors (GitHub
    mutations via ``refuse_if_agent``, MCP's elicitation callback) keep
    refusing to act or answer on a human's behalf.

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
        if unattended:
            call_input["__agent__"] = True

        # --- Pre-execution checks ---

        # Agent tool scope: a name outside the run's entitlement never runs.
        if tool_scope is not None and tool_name not in tool_scope.permitted:
            state.status = "skipped"
            state.output = (
                f"[Refused] '{tool_name}' is not one of this agent's tools, so it did "
                "not run. Continue with the tools you have."
            )
            log.info("agent tool scope refused %s", tool_name)
            tool_scope.refused()
            return

        # Sibling abort: skip writes if a command tool failed
        if command_error and not is_concurrent_safe(tool_name):
            state.status = "skipped"
            state.output = f"[Skipped] Prior command failed: {command_error}"
            return

        # Plan mode block — emit SSE so frontend can offer the upgrade dialog
        if plan_mode and tool_name in _PLAN_MODE_BLOCKED:
            state.status = "skipped"
            state.output = (
                f"[Plan Mode] Tool '{tool_name}' is blocked while plan mode is active. "
                "The tool stays advertised so the catalog is stable; it is refused "
                "here at execution. Design the plan and call create_plan instead; "
                "the user exits plan mode to apply it."
            )
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

        # Denial tracking (bypassPermissions is "no prompts, no blocks"; a stale
        # denial count from an earlier mode must not hold under it). The count
        # is of refused cards, so a call that starts with no card at all (an
        # approved launch.json preview with no floor or asking rule) is not
        # held back by it; one that would still show a card is. Its real
        # decision is still made in process_tool_results.
        if mode != MODE_BYPASS and not never_prompts(tool_name, state.tool_use.get("input") or {}):
            denials = session_denials.get(tool_name, 0)
            if denials >= MAX_AUTO_DENIALS:
                state.status = "skipped"
                state.output = f"[Denied] '{tool_name}' has been denied {denials} times. Re-enable in permissions settings to use again."
                return

        # --- Loop guard: identical repeats, repeated failures, cycles, caps ---
        if guard_scope:
            from server.chat import loop_guard

            _guard = loop_guard.before_call(guard_scope, tool_name, hook_input)
            if not _guard.allow:
                state.status = "skipped"
                state.output = _guard.message
                log.info("loop guard refused %s (%s)", tool_name, _guard.reason)
                if tool_scope is not None:
                    tool_scope.refused()
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
            if unattended:
                call_input["__agent__"] = True

        # --- Dispatch ---
        if tool_scope is not None:
            tool_scope.ran()
        # An agent this call starts inherits the turn's plan mode (run_agent
        # makes it read-only); reset after, so the flag never outlives it.
        from server.agents.tool_access import plan_mode_scope

        _plan_token = plan_mode_scope.set(plan_mode)
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
                event_channel=event_channel,
                tool_scope=tool_scope,
            )
            output = _explain_lost_workspace(output)
            if isinstance(output, str) and output.startswith("[WS_APPROVAL]"):
                from server.workspace.state import load_workspace_config

                state.ws_root = load_workspace_config().get("path")
                state.ws_root_taken = True
            state.output = output
            state.side_effects = side_effects
            state.status = "completed"

            if guard_scope:
                from server.chat import loop_guard

                _is_err = isinstance(output, str) and output.startswith(
                    ("[Tool Error]", "Error:", "[Denied", "[Skipped]")
                )
                _notice, _stub = loop_guard.after_call(
                    guard_scope, tool_name, hook_input, state.output, is_error=_is_err
                )
                if _stub:
                    state.output = _stub
                if _notice:
                    state.output = f"{state.output}{_notice}"

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
            if guard_scope:
                from server.chat import loop_guard

                loop_guard.after_call(
                    guard_scope, tool_name, hook_input, state.output, is_error=True
                )

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
        finally:
            plan_mode_scope.reset(_plan_token)

    # --- Execute batches ---
    from server.workspace.state import reset_turn_latch, set_turn_latch

    latch_token = set_turn_latch(workspace_latch)
    try:
        batches = partition_batches(tool_uses, is_concurrent_safe)
        for batch in batches:
            batch_states = [state_by_id[tu["id"]] for tu in batch]
            if len(batch_states) == 1:
                await _run_one(batch_states[0])
            else:
                await asyncio.gather(*[_run_one(s) for s in batch_states])
    finally:
        reset_turn_latch(latch_token)

    return states


def _explain_lost_workspace(output):
    """Give a bare "No workspace connected" the full reason when the turn's
    workspace was disconnected mid-turn, so the model stops retrying and tells
    the user instead. Executors keep their one-line refusal; the gate adds
    the why, the same way it adds plan-mode and loop-guard reasons."""
    if not isinstance(output, str) or not output.startswith(
        ("No workspace connected", "Error: No workspace connected")
    ):
        return output
    from server.workspace.state import workspace_lost_message

    return workspace_lost_message() or output


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
    # A command that ran and failed carries its output next to the exit code.
    error = f"Error: {outcome.error or 'unknown error'}"
    return f"{error}\n\n{outcome.output}" if outcome.output else error


def _bind_to_workspace(spec, state: ToolState, ws_parsed: dict) -> str | None:
    """Stamp a workspace-bound payload with the root it was resolved against,
    then return why the turn may not run it now (or None).

    The root is the one taken when the tool returned (ToolState.ws_root); a
    state that never ran through execute_tool_batch falls back to the root
    connected now."""
    if not spec.binds_workspace(ws_parsed):
        return None
    if getattr(state, "ws_root_taken", False):
        root = state.ws_root
    else:
        from server.workspace.state import load_workspace_config

        root = load_workspace_config().get("path")
    ws_parsed[WORKSPACE_ROOT_KEY] = root
    return turn_workspace_refusal(spec, ws_parsed)


async def process_tool_results(
    states: list[ToolState],
    budget_fn: callable,
    *,
    session_approvals: dict | None = None,
    config: dict | None = None,
    model_id: str = "",
    recent_messages: list[dict] | None = None,
    mode: str = "default",
    session_id: str = "",
    cost_session_id: str = "",
    unattended: bool = False,
    workspace_latch=None,
) -> tuple[list[dict], list[str], bool, bool]:
    """Post-process completed tool states into Bedrock messages and SSE events.

    Args:
        states: Completed ToolState objects in request order.
        budget_fn: Function (tool_name, output) -> budgeted_output for large results.
        session_approvals: Category-level pre-approvals from frontend (e.g. {"write": "allow"}).
        session_id: Keys the auto-mode circuit breaker (server.security.permissions).
            Empty is treated as "no breaker tracking" (matches existing test callers
            that don't pass one). A turn scope (TurnContext.turn_scope_id), which
            is not the chat session for a delegated voice or headless turn.
        cost_session_id: The chat session the classifier and explainer calls
            are billed to in the cost log, the one the turn's own rounds are
            logged under, so they count toward its budget and readout.
        unattended: True for a turn with no human present (subagents). Every
            pause-inducing outcome resolves to a refusal instead of a pause:
            [WS_APPROVAL] executes immediately (agent=True, unconditional —
            no category gate, no classifier), [WS_WORKSPACE_PROMPT] and any
            ask_user_question-style pause (SIDE_EFFECT_PAUSE) become a plain
            error tool_result. has_pending_approval/has_user_question stay
            False in this mode by construction — there is nothing to resume,
            since no continuation turn from a human is coming.

        workspace_latch: The turn's server.workspace.state.WorkspaceLatch, the
            same one execute_tool_batch ran the batch under. Everything here
            runs under it too: a pre-approved or unattended action executes
            in this call, and a workspace-bound one whose workspace the user
            let go of is refused with the reason instead of acting on the
            folder connected now (approval.spec.turn_workspace_refusal).

    Returns:
        (tool_results, sse_events, has_pending_approval, has_user_question)
        - tool_results: List of tool_result dicts for Bedrock messages
        - sse_events: List of SSE event strings (already ndjson-encoded)
        - has_pending_approval: True if any tool needs user approval before the LLM continues
        - has_user_question: True if any tool triggered a pause
    """
    from server.workspace.state import reset_turn_latch, set_turn_latch

    latch_token = set_turn_latch(workspace_latch)
    try:
        return await _process_tool_results(
            states,
            budget_fn,
            session_approvals=session_approvals,
            config=config,
            model_id=model_id,
            recent_messages=recent_messages,
            mode=mode,
            session_id=session_id,
            cost_session_id=cost_session_id,
            unattended=unattended,
        )
    finally:
        reset_turn_latch(latch_token)


async def _process_tool_results(
    states: list[ToolState],
    budget_fn: callable,
    *,
    session_approvals: dict | None,
    config: dict | None,
    model_id: str,
    recent_messages: list[dict] | None,
    mode: str,
    session_id: str,
    cost_session_id: str,
    unattended: bool,
) -> tuple[list[dict], list[str], bool, bool]:
    """process_tool_results' body, run under the turn's workspace latch."""
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
        _pause_signaled = False
        for effect in state.side_effects:
            if SIDE_EFFECT_PAUSE in effect:
                _pause_signaled = True
                if not unattended:
                    has_user_question = True
                continue
            sse_events.append(ndjson_dumps(effect))

        tool_output = state.output

        # Unattended (agent) turns: a tool that signaled SIDE_EFFECT_PAUSE
        # (ask_user_question, or any future pause-inducing tool) has no human
        # to actually answer it. Replace its "[PAUSE] ..." placeholder output
        # with a refusal so the model can adapt and keep working instead of
        # stalling forever waiting for a reply that will never arrive.
        if unattended and _pause_signaled:
            tool_output = (
                "Error: cannot ask the user a question unattended. Make your "
                "best judgment, state your assumption, and continue."
            )

        # Workspace-prompt detection: a write-type tool fired without a
        # workspace (or against a path outside the current one). Show a
        # folder picker to the user; stream resumes on continuation turn.
        if isinstance(tool_output, str) and tool_output.startswith("[WS_WORKSPACE_PROMPT]"):
            if unattended:
                # No human to show a folder picker to — refuse and keep going.
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": (
                            "Error: no workspace connected and cannot prompt "
                            "the user unattended. Open a workspace first, or "
                            "ask the parent to."
                        ),
                    }
                )
                continue
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
            # A workspace-bound payload is stamped with the root it was
            # resolved against, and refused if, since the tool ran, the user
            # let go of the turn's workspace or that root stopped being the
            # connected one: no card for it, and no pre-approved or unattended
            # run in the folder connected now. A card's Yes is checked against
            # the same stamp (approval.spec.stale_workspace_refusal).
            refusal = _bind_to_workspace(spec, state, ws_parsed)
            if refusal:
                tool_output = refusal
                # Fall through to normal result processing below
            elif unattended:
                # Unattended (agent) turns auto-approve unconditionally — no
                # human, no classifier, no category gate. Mirrors the
                # pre-migration server/agents/runtime.py inline handling
                # exactly (agent=True stamps the payload so high-blast-radius
                # executors can still refuse to run unattended).
                result = await _execute_ws_approval_inline(ws_parsed, agent=True)
                sse_events.append(ndjson_dumps({"ws_auto_applied": ws_parsed}))
                tool_output = result
                # Fall through to normal result processing below
            else:
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

                # Precedence: rm guardrail → github-destructive/MCP-approve →
                # category mode override → bypassPermissions → trusted skill →
                # session approvals → custom rules → approved launch.json
                # preview (allow) → dontAsk → acceptEdits → auto (classifier) →
                # ask. See server.security.permissions.resolve_static_decision
                # (the rm guardrail lives there, alongside github-destructive,
                # since both are "no bypass mode may cover this" absolutes).
                decision = resolve_static_decision(
                    state.tool_name,
                    ws_parsed,
                    category,
                    session_approvals,
                    mode,
                    auto_allow_trusted,
                )
                if decision is None:
                    # mode == "auto" and nothing else resolved it — selecting the
                    # Auto permission mode IS the opt-in, so the classifier runs
                    # here with no separate enable flag. Only actions that actually
                    # need approval reach this point (read-only tools resolve
                    # earlier). model_id is empty for the offline local-model path,
                    # which must stay Bedrock-free and falls back to asking.
                    cfg = config or {}
                    if model_id:
                        from server.security.permissions import (
                            is_auto_mode_tripped,
                            record_classifier_verdict,
                        )

                        if session_id and is_auto_mode_tripped(session_id):
                            # Circuit breaker already tripped this turn (see
                            # record_classifier_verdict below) — stop consulting
                            # the classifier and ask for every remaining
                            # approval-gated call until the user resumes auto
                            # mode (POST /api/permissions/auto-mode/resume) or a
                            # new turn starts.
                            decision = "ask"
                        else:
                            verdict = await classify_tool_call(
                                state.tool_name,
                                ws_parsed,
                                cfg,
                                recent_messages=recent_messages or [],
                                session_id=cost_session_id,
                            )
                            decision = "allow" if verdict.get("decision") == "allow" else "ask"
                            if session_id:
                                breaker = record_classifier_verdict(
                                    session_id, allowed=(decision == "allow")
                                )
                                if breaker["tripped"]:
                                    sse_events.append(
                                        ndjson_dumps(
                                            {"auto_mode_breaker": {"reason": breaker["reason"]}}
                                        )
                                    )
                    else:
                        decision = "ask"

                if decision == "allow":
                    # The auto-mode classifier may have awaited since the
                    # check above. A disconnect or a switch in that window
                    # refuses here, before a payload meant for the old root
                    # runs in the folder connected now.
                    late_refusal = turn_workspace_refusal(spec, ws_parsed)
                    if late_refusal:
                        tool_output = late_refusal
                        # Fall through to normal result processing below
                    else:
                        # Pre-approved: execute inline and return result to LLM
                        log.info("approval: executing inline (pre-approved)")
                        from server.workspace import get_workspace_path

                        ws_before = get_workspace_path()
                        result = await _execute_ws_approval_inline(ws_parsed)
                        # Auto-applied event keeps the file tree / editor in sync.
                        sse_events.append(ndjson_dumps({"ws_auto_applied": ws_parsed}))
                        # If the action switched the workspace (e.g. git_clone with
                        # open=true), tell the frontend to open the panel. Detected by
                        # diffing the connected path, the same generic signal the
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
                            session_id=cost_session_id,
                        )

                    # The explanation can take seconds. A disconnect or a
                    # switch meanwhile refuses the call here, rather than
                    # pausing the turn on a card whose Yes would be refused.
                    late_refusal = turn_workspace_refusal(spec, ws_parsed)
                    if late_refusal:
                        tool_output = late_refusal
                        # Fall through to normal result processing below
                    else:
                        # Build the generic approval_request event from the spec.
                        # No more per-action shape: the frontend reads `preview`
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
                                # A hard floor asks every time, whatever the
                                # session remembers, so the card must not offer
                                # "Yes, all" / "Block" for it.
                                "always_asks": approval_floor(state.tool_name, ws_parsed, category)
                                is not None,
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
