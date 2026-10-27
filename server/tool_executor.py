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
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import NamedTuple

from server.approval.markers import (
    APPROVAL,
    PREVIEW_IMAGE,
    WORKSPACE_PROMPT,
    as_plain_text,
    is_app_request,
)
from server.hooks import run_hooks
from server.preview.start_policy import never_prompts
from server.security.permissions import MODE_BYPASS
from server.security.secret_reads import SECRET_READS_KEY
from server.tool_approvals import PAUSE as APPROVAL_PAUSE
from server.tool_approvals import REPLY as APPROVAL_REPLY
from server.tool_approvals import gate_approval
from server.tool_meta import result_meta
from server.tool_router import SIDE_EFFECT_PAUSE, route_tool
from server.utils import ndjson_dumps

# What a paused ask_user_question's tool_result says until the answer arrives,
# and what the continuation puts there when it brought no answer for it
# (server/chat/routes._resume_messages).
ASK_PAUSE_MARKER = "[ASK_USER_PAUSE] Awaiting answer; will be replaced on continuation."
ASK_UNANSWERED = (
    "[No answer] The user did not answer this question. Ask it again if you still need the answer."
)
# What the row of a call that needed a workspace says while the folder card
# waits: the call never ran. The model's own placeholder for it is replaced
# by the card's answer on the continuation (routes._resume_messages).
WS_PROMPT_NOT_RUN = "[Not executed] No workspace was connected, so this call did not run."

log = logging.getLogger("whisper-studio")

# Tools whose errors cascade to subsequent writes
_COMMAND_TOOLS = {"ws_run_command", "run_python"}

# Write tools blocked in plan mode. The app's own folder is not the
# workspace: memory_write and memory_delete keep running in plan mode with no
# card (the user's rule), and rules_write runs there behind an approval card
# it raises itself (server/rules_tools.py), so neither is listed.
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

# What a tool result opens with when the call did not do its job: the gate's
# own markers (a tool exception, a denial, a sibling skipped after a failed
# command, a tool outside an agent's scope, plan mode, a hook, the loop
# guard, a call canceled by another call's pause, a payload the gate could
# not read) and an executor's own error line. One list for the loop guard's
# repeated-failure count and the skill_result frame's is_error flag.
_ERROR_PREFIXES = (
    "[Tool Error]",
    "[Error]",
    "Error:",
    "Error ",
    "error:",
    "[Denied",
    "[Skipped]",
    "[Refused]",
    "[Plan Mode]",
    "[Hook denied]",
    "[Loop guard]",
    "[Not executed]",
)
# Where a command tool writes its own exit status, one fixed place each, so a
# file, a diff or a log that merely mentions an exit code is never read as
# one. ws_run_command (server/workspace/executors.py run_workspace_command)
# ends a finished run that failed with "(exit code N)" or "(exit code N:
# meaning)" on a line of its own, followed at most by the "[Left running]"
# paragraph; a run that exited 0 says nothing.
_RUN_EXIT_RE = re.compile(r"\n\(exit code (-?\d+)(?::[^\n]*)?\)(?:\n\[Left running\][^\n]*)?\Z")
# A ws_run_command that never finished in the foreground has no exit status:
# a background start or handoff, no workspace (or one the user let go of),
# no command.
_RUN_UNFINISHED = ("[Background Task Started]", "No workspace connected", "No command provided")
# aws_cli (server/executors/code.py do_aws_cli) opens a failure with its exit
# status, after the gate's "Error: " when the gate ran it inline.
_AWS_EXIT_RE = re.compile(r"(?:Error: )?\(exit code (-?\d+)\)")
# What the gate appends after a tool returned: a PostToolUse hook's context
# (_append_hook_context), and the loop guard's note before it.
_HOOK_NOTE = "\n\n[Hook] "
_LOOP_NOTE_RE = re.compile(r"\n\n\[Loop guard note: [^\n]*\]\Z")


class ToolOutcome(NamedTuple):
    is_error: bool
    exit_code: int | None


def _executor_text(text: str) -> str:
    """``text`` without the notes the gate appended after the tool returned,
    so an exit status at the end of the executor's own text is found."""
    hook = text.rfind(_HOOK_NOTE)
    if hook != -1:
        text = text[:hook]
    note = _LOOP_NOTE_RE.search(text)
    return text[: note.start()] if note else text


def tool_outcome(tool_name: str, output) -> ToolOutcome:
    """Whether a tool result reports a failure, and the exit code it carries.

    Read on the FULL output, never a preview. ``is_error`` is true when the
    output opens with one of the gate's error markers or reports a non-zero
    exit code. Only a command tool reports an exit code, read where its
    executor writes it and nowhere else: ws_run_command's last line (0 when
    a finished run has none and no error marker, since its executor only
    says so on failure; None when it never finished in the foreground) and
    aws_cli's first line on a failure. Every other tool's code is None, and
    its content never makes it an error. The loop guard and the skill_result
    frame share this one rule."""
    text = output if isinstance(output, str) else str(output)
    prefixed = text.startswith(_ERROR_PREFIXES)
    exit_code: int | None = None
    if tool_name == "ws_run_command":
        found = _RUN_EXIT_RE.search(_executor_text(text))
        if found:
            exit_code = int(found.group(1))
        elif not prefixed and not text.startswith(_RUN_UNFINISHED):
            exit_code = 0
    elif tool_name == "aws_cli":
        found = _AWS_EXIT_RE.match(text)
        if found:
            exit_code = int(found.group(1))
    return ToolOutcome(prefixed or bool(exit_code), exit_code)


def _result_frame(
    state: "ToolState", output: str, preview: str, *, sent: str | None = None, **extra
) -> str:
    """The skill_result frame: the preview the UI shows, the id that pairs it
    with its skill frame, the outcome read from the FULL output, and the meta
    (server/tool_meta.py). ``sent`` is the budgeted text the model receives
    when the budget cut the output; None means it received ``output``."""
    outcome = tool_outcome(state.tool_name, output)
    sent_output = output if sent is None else sent
    return ndjson_dumps(
        {
            "skill_result": state.tool_name,
            "output": preview,
            "tool_use_id": state.tool_id,
            "is_error": outcome.is_error,
            "exit_code": outcome.exit_code,
            "meta": result_meta(state, output, sent_output, is_error=outcome.is_error),
            **extra,
        }
    )


def _reply(state: "ToolState", text: str, tool_results: list, sse_events: list) -> None:
    """Answer ``state`` with ``text`` as is (no budget, it is the gate's own
    line): the tool_result the model reads and the skill_result frame its row
    shows, so no call is left looking as if it ran."""
    sse_events.append(_result_frame(state, text, text))
    tool_results.append({"type": "tool_result", "tool_use_id": state.tool_id, "content": text})


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
    # How long route_tool took (skill_result meta.duration_ms); None for a
    # call that never ran.
    duration_ms: int | None = None
    # The diff stats of a write this gate ran inline (ApprovalOutcome.stats),
    # for the same meta; None for every other call.
    stats: dict | None = None

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
        # Whether this call may read the credential stores without a card
        # (server/security/secret_reads.py): only in bypassPermissions, and
        # always set, so a value the model sent never counts.
        call_input[SECRET_READS_KEY] = mode == MODE_BYPASS

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
        # makes it read-only); a read-only scope already on stays on. Reset
        # after, so the flag never outlives the call.
        from server.agents.tool_access import WAITS_ON_AGENTS, read_only_scope

        _ro_token = read_only_scope.set(plan_mode or read_only_scope.get())
        # An agent waiting on agents it started is not using its own time:
        # each of them has its own limits (ToolScope.waited).
        _waits = tool_scope is not None and tool_name in WAITS_ON_AGENTS
        _started = time.monotonic()
        try:
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
            finally:
                # Measured around the dispatch alone: the hooks and the gate
                # are not the tool's time. Kept on a failure too.
                _elapsed = time.monotonic() - _started
                state.duration_ms = int(_elapsed * 1000)
                if _waits:
                    tool_scope.waited(_elapsed)
            # Text from outside the app (an MCP server's reply, a file, a
            # cached result) that starts with an app marker stays text: only
            # an AppRequest the app made is a request (server/approval/markers.py).
            output = as_plain_text(_explain_lost_workspace(output))
            if is_app_request(output, APPROVAL):
                from server.workspace.state import load_workspace_config

                state.ws_root = load_workspace_config().get("path")
                state.ws_root_taken = True
            state.output = output
            state.side_effects = side_effects
            state.status = "completed"

            if guard_scope:
                from server.chat import loop_guard

                _notice, _stub = loop_guard.after_call(
                    guard_scope,
                    tool_name,
                    hook_input,
                    state.output,
                    is_error=tool_outcome(tool_name, state.output).is_error,
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
            read_only_scope.reset(_ro_token)

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
    # The call the turn paused on for a card, and the folder card's frame:
    # it goes out last, after every row's result, since the client commits
    # the rows above the card when it arrives.
    paused_id: str | None = None
    prompt_frame: str | None = None

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
        # workspace to resolve its relative path against. Show a folder
        # picker to the user; stream resumes on continuation turn.
        if is_app_request(tool_output, WORKSPACE_PROMPT):
            if unattended:
                # No human to show a folder picker to — refuse and keep going.
                _reply(
                    state,
                    "Error: no workspace connected and cannot prompt "
                    "the user unattended. Open a workspace first, or "
                    "ask the parent to.",
                    tool_results,
                    sse_events,
                )
                continue
            prompt_data = tool_output[len(WORKSPACE_PROMPT) :]
            try:
                prompt_parsed = json.loads(prompt_data)
            except json.JSONDecodeError as e:
                log.error("Malformed [WS_WORKSPACE_PROMPT] payload: %s", e)
                _reply(
                    state,
                    f"[Error] Malformed workspace-prompt payload: {e}",
                    tool_results,
                    sse_events,
                )
                continue
            prompt_parsed = {**prompt_parsed, "tool_use_id": state.tool_id}
            sse_events.append(_result_frame(state, WS_PROMPT_NOT_RUN, WS_PROMPT_NOT_RUN))
            prompt_frame = ndjson_dumps({"ws_workspace_prompt": prompt_parsed})
            has_pending_approval = True
            paused_id = state.tool_id
            break

        # Workspace approval detection. Tools signal "needs approval" by
        # returning an AppRequest: the [WS_APPROVAL] marker and a JSON payload
        # (server/approval/markers.py approval_request);
        # server/tool_approvals.py refuses it, runs it inline, denies it, or
        # emits the approval card the turn pauses on.
        if is_app_request(tool_output, APPROVAL):
            step = await gate_approval(
                state,
                tool_output,
                sse_events,
                session_approvals=session_approvals,
                config=config,
                model_id=model_id,
                recent_messages=recent_messages,
                mode=mode,
                session_id=session_id,
                cost_session_id=cost_session_id,
                unattended=unattended,
            )
            if step.kind == APPROVAL_PAUSE:
                has_pending_approval = True
                # Its row waits for the card's answer (the client patches it).
                paused_id = state.tool_id
                break  # Do not execute sibling tools in this batch.
            if step.kind == APPROVAL_REPLY:
                _reply(state, step.text, tool_results, sse_events)
                continue
            # An inline run's result or a refusal: processed like any output.
            tool_output = step.text

        # Preview screenshot detection: preview_screenshot's executor returns
        # a sentinel carrying base64 JPEG bytes + a caption. Unlike every
        # other tool (whose content is a plain string), this ONE tool_result
        # must be a list of Anthropic content blocks — an image block plus a
        # sibling text block — so the model actually sees the pixels, not a
        # wall of base64 text. Matches the shape already used for
        # user-uploaded image attachments (server/chat/routes.py). Placed
        # before budget_fn below: truncating a base64 payload would corrupt
        # the image.
        if is_app_request(tool_output, PREVIEW_IMAGE):
            img_data = tool_output[len(PREVIEW_IMAGE) :]
            try:
                img_parsed = json.loads(img_data)
            except json.JSONDecodeError as e:
                log.error("Malformed [WS_PREVIEW_IMAGE] payload: %s", e)
                _reply(
                    state,
                    f"[Error] Malformed preview-image payload: {e}",
                    tool_results,
                    sse_events,
                )
                continue
            caption = img_parsed.get("caption", "Screenshot")
            media_type = img_parsed.get("media_type", "image/jpeg")
            data = img_parsed.get("data", "")
            sse_events.append(
                _result_frame(
                    state,
                    caption,
                    caption,
                    # Frontend-only field: PreviewScreenshotCard keys off this
                    # to render the <img>; other skill_result consumers ignore it.
                    preview_image={"media_type": media_type, "data": data},
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
        full_output = tool_output if isinstance(tool_output, str) else str(tool_output)
        if isinstance(tool_output, str):
            from server.skills import produces_model_prompt

            if not produces_model_prompt(state.tool_name):
                tool_output = budget_fn(state.tool_name, tool_output)

        # Emit result preview as SSE; the outcome is read from the full
        # output, before the budget cut it, and the meta sees both texts.
        if not isinstance(tool_output, str):
            tool_output = str(tool_output)
        preview = tool_output[:2000] + ("..." if len(tool_output) > 2000 else "")
        sse_events.append(_result_frame(state, full_output, preview, sent=tool_output))

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
            awaiting_approval = is_app_request(out, APPROVAL) or is_app_request(
                out, WORKSPACE_PROMPT
            )
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
                sse_events.append(_result_frame(state, out, preview, sent=content))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": content,
                    }
                )
            elif state.tool_id == paused_id:
                # The paused call's row is answered by its card; the folder
                # card's call already has its frame.
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": state.tool_id,
                        "content": not_executed_msg,
                    }
                )
            else:
                _reply(state, not_executed_msg, tool_results, sse_events)
        if prompt_frame:
            sse_events.append(prompt_frame)
    elif has_user_question:
        # For ask_user_question: the tool's output is already "[PAUSE] …"
        # text appended to tool_results, but Bedrock hasn't seen the user's
        # actual answer yet. Replace each ask_user_question placeholder with
        # an explicit "awaiting answer" marker that the resume path will
        # rewrite when the user submits.
        # The runner logs nothing for a paused round, so this is the one line
        # that tells a log reader where a question stopped the turn.
        log.info("Paused on a user question: %d tool result(s) await the answer", len(states))
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
