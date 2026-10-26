"""The approval branch of the tool gate, split out of server/tool_executor.py
(process_tool_results) along its topic so both files stay under the line
budget.

A tool that needs approval returns a ``[WS_APPROVAL]`` sentinel carrying JSON.
The frontend never sees the prefix: ``gate_approval`` reads the payload
against the ApprovalSpec registered for its action and either refuses it, runs
it inline, denies it by session rule, or emits the generic
``approval_request`` frame and pauses the turn. A pre-approved action runs
through ``_execute_ws_approval_inline``, the same ApprovalSpec executor the
card's Yes runs, so there is one execution path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import NamedTuple

from server.approval.markers import APPROVAL
from server.approval.spec import WORKSPACE_ROOT_KEY, ApprovalOutcome, turn_workspace_refusal
from server.auto_mode import classify_tool_call
from server.security.explainer import explain_permission
from server.security.permissions import (
    FILE_WRITE_CATEGORIES,
    approval_floor,
    resolve_static_decision,
    targets_app_config,
)
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

# What the gate does with the call after gate_approval (ApprovalStep.kind).
REPLY = "reply"  # ``text`` is its answer as is: tool_result and frame, unbudgeted
OUTPUT = "output"  # ``text`` is its output from here on: budgeted and framed
PAUSE = "pause"  # an approval card went out: the turn pauses, no sibling runs


class ApprovalStep(NamedTuple):
    kind: str
    text: str = ""


async def _execute_ws_approval_inline(
    ws_parsed: dict, *, agent: bool = False, tool_use_id: str = ""
) -> ApprovalOutcome:
    """Run a pre-approved action through the same ApprovalSpec executor the
    UI's Yes button uses. Single execution path: no duplicated write/delete/
    command logic. Adding a new approval-required tool requires only a
    `register()` call, not a branch here. Returns the executor's outcome; the
    text the model reads is inline_result_text, and a write's stats ride on
    the outcome to the skill_result meta.

    `agent=True` marks the call as originating from a subagent's UNCONDITIONAL
    auto-approve path (server/agents/runtime.py): no human, no category gate.
    It stamps `__agent__` onto the payload so high-blast-radius executors (e.g.
    GitHub mutations) can refuse to run unattended. The chat/auto-mode caller
    leaves this False, because there a mode, a session answer, a rule or the
    auto-mode classifier authorised the action; every payload through here is
    stamped `__unreviewed__` (server.approval.spec.UNREVIEWED_KEY), since no
    person saw this exact action on a card, and an executor that confines
    what it runs reads that stamp (a command runs workspace-write confined
    unless a person approved it).

    `tool_use_id` is the call's id (ToolState.tool_id): a write keeps its edit
    under it for the diff dialog (server/workspace/edits.py)."""
    from server.approval import registry as approval_registry
    from server.approval.spec import UNREVIEWED_KEY

    action = ws_parsed.get("action", "write")
    spec = approval_registry.get(action)
    if not spec:
        return ApprovalOutcome(
            ok=False, error=f"unknown action '{action}' (no ApprovalSpec registered)"
        )
    payload = spec.build_payload(ws_parsed)
    # Forward session_id if present in the original parsed payload so
    # executors that need it (worktree enter/exit) can read it.
    if "session_id" in ws_parsed and "session_id" not in payload:
        payload["session_id"] = ws_parsed["session_id"]
    # Stamp the origin and the call id AFTER build_payload (which whitelists
    # fields when payload_fields is set and would otherwise drop them).
    payload[UNREVIEWED_KEY] = True
    if agent:
        payload["__agent__"] = True
    if tool_use_id:
        payload["__tool_use_id__"] = tool_use_id
    try:
        return await spec.executor(payload)
    except Exception as e:  # noqa: BLE001
        return ApprovalOutcome(ok=False, error=f"Executor crashed: {e}")


async def _run_inline(state, ws_parsed: dict, *, agent: bool = False) -> ApprovalOutcome:
    """Run ``ws_parsed`` inline for ``state`` and keep what the meta needs:
    the write's stats, and the run's time added to the call's duration
    (route_tool only returned the sentinel; the action itself runs here)."""
    started = time.monotonic()
    try:
        outcome = await _execute_ws_approval_inline(
            ws_parsed, agent=agent, tool_use_id=state.tool_id
        )
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        state.duration_ms = (getattr(state, "duration_ms", None) or 0) + elapsed_ms
    state.stats = outcome.stats
    return outcome


def inline_result_text(action: str, outcome: ApprovalOutcome) -> str:
    """The tool result the model reads for an action the gate ran inline."""
    if outcome.ok:
        return outcome.output or f"[OK] {action}"
    # A command that ran and failed carries its output next to the exit code.
    error = f"Error: {outcome.error or 'unknown error'}"
    return f"{error}\n\n{outcome.output}" if outcome.output else error


def _bind_to_workspace(spec, state, ws_parsed: dict) -> str | None:
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


async def gate_approval(
    state,
    tool_output: str,
    sse_events: list[str],
    *,
    session_approvals: dict,
    config: dict | None,
    model_id: str,
    recent_messages: list[dict] | None,
    mode: str,
    session_id: str,
    cost_session_id: str,
    unattended: bool,
) -> ApprovalStep:
    """Decide what happens to ``state``, whose output is the ``[WS_APPROVAL]``
    sentinel ``tool_output``. Frames it emits (ws_auto_applied,
    ws_folder_opened, auto_mode_breaker, approval_request) go on
    ``sse_events``; an action it runs inline adds its run time to
    ``state.duration_ms`` and leaves a write's diff stats on ``state.stats``,
    both for the skill_result meta. The keywords are
    process_tool_results' own (server/tool_executor.py)."""
    from server.approval import registry as approval_registry

    ws_data = tool_output[len(APPROVAL) :]
    try:
        ws_parsed = json.loads(ws_data)
    except json.JSONDecodeError as e:
        log.error("Malformed [WS_APPROVAL] payload: %s", e)
        return ApprovalStep(REPLY, f"[Error] Malformed approval payload: {e}")
    action = ws_parsed.get("action", "")
    spec = approval_registry.get(action)
    if not spec:
        log.error("approval: no ApprovalSpec registered for action=%r, refusing", action)
        return ApprovalStep(
            REPLY,
            f"[Error] No approval registered for action {action!r}. "
            f"Refusing to run. Register an ApprovalSpec in server/approval/bootstrap.py.",
        )
    # A workspace-bound payload is stamped with the root it was
    # resolved against, and refused if, since the tool ran, the user
    # let go of the turn's workspace or that root stopped being the
    # connected one: no card for it, and no pre-approved or unattended
    # run in the folder connected now. A card's Yes is checked against
    # the same stamp (approval.spec.stale_workspace_refusal).
    refusal = _bind_to_workspace(spec, state, ws_parsed)
    if refusal:
        return ApprovalStep(OUTPUT, refusal)
    if unattended:
        # A write to the app's own grant or config files follows the permission
        # mode (the app-config table), so a person decides it. An unattended
        # run has none, so it is refused rather than auto-approved.
        if spec.category in FILE_WRITE_CATEGORIES and targets_app_config(ws_parsed):
            return ApprovalStep(
                OUTPUT,
                "[Error] A write to the app's own grant or config files needs a "
                "person's approval, and this run has no person to ask. Ask the "
                "top-level session, where a person can approve it, to make this change.",
            )
        # Unattended (agent) turns auto-approve unconditionally: no
        # human, no classifier, no category gate. Mirrors the
        # pre-migration server/agents/runtime.py inline handling
        # exactly (agent=True stamps the payload so high-blast-radius
        # executors can still refuse to run unattended).
        outcome = await _run_inline(state, ws_parsed, agent=True)
        sse_events.append(ndjson_dumps({"ws_auto_applied": ws_parsed}))
        return ApprovalStep(OUTPUT, inline_result_text(action, outcome))

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

    # Precedence: the hard floors (github-destructive, a delete outside the
    # workspace, sandbox escalation, a visible terminal run, MCP-approve), app
    # storage, category mode override, bypassPermissions (not for a save or
    # folder reach outside the workspace), trusted skill, session approvals,
    # custom rules, approved
    # launch.json preview (allow), dontAsk, acceptEdits, auto (classifier),
    # ask. See server.security.permissions.resolve_static_decision.
    decision = resolve_static_decision(
        state.tool_name,
        ws_parsed,
        category,
        session_approvals,
        mode,
        auto_allow_trusted,
    )
    if decision is None:
        # mode == "auto" and nothing else resolved it: selecting the
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
                # record_classifier_verdict below): stop consulting
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
                    breaker = record_classifier_verdict(session_id, allowed=(decision == "allow"))
                    if breaker["tripped"]:
                        sse_events.append(
                            ndjson_dumps({"auto_mode_breaker": {"reason": breaker["reason"]}})
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
            return ApprovalStep(OUTPUT, late_refusal)
        # Pre-approved: execute inline and return result to LLM
        log.info("approval: executing inline (pre-approved)")
        from server.workspace import get_workspace_path

        ws_before = get_workspace_path()
        outcome = await _run_inline(state, ws_parsed)
        # Auto-applied event keeps the file tree / editor in sync.
        sse_events.append(ndjson_dumps({"ws_auto_applied": ws_parsed}))
        # If the action switched the workspace (e.g. git_clone with
        # open=true), tell the frontend to open the panel. Detected by
        # diffing the connected path, the same generic signal the
        # manual approval route uses, so no per-action branch here.
        ws_after = get_workspace_path()
        if ws_after and ws_after != ws_before:
            sse_events.append(ndjson_dumps({"ws_folder_opened": ws_after}))
        return ApprovalStep(OUTPUT, inline_result_text(action, outcome))
    if decision == "deny":
        path = ws_parsed.get("path", ws_parsed.get("command", ""))
        return ApprovalStep(REPLY, f"[Denied by session rule] {action}: {path}, blocked by user.")

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
        return ApprovalStep(OUTPUT, late_refusal)

    # Build the generic approval_request event from the spec.
    # No more per-action shape: the frontend reads `preview`
    # and picks one of four renderers (diff/command/list/text).
    payload = spec.build_payload(ws_parsed)
    summary = spec.render_summary(ws_parsed)
    event = {
        "approval_request": {
            "tool_use_id": state.tool_id,
            "action": action,
            "category": category,
            "preview": spec.preview,
            "summary": summary,
            "payload": payload,
            "risk_hint": spec.risk_hint,
            "explanation": explanation,
            # A hard floor asks every time, whatever the
            # session remembers, so the card must not offer
            # "Yes, all" / "Block" for it. A write to the app's own
            # grant or config files is the same: it never falls under a
            # blanket "yes for all writes", so the card must not offer it.
            "always_asks": (
                approval_floor(state.tool_name, ws_parsed, category) is not None
                or (category in FILE_WRITE_CATEGORIES and targets_app_config(ws_parsed))
            ),
        }
    }
    sse_events.append(ndjson_dumps(event))

    # Pause the stream. All approval actions (command, write,
    # create, delete) wait for the user. After approval, the
    # frontend sends a new /api/chat turn carrying the real
    # tool_result so the LLM resumes with truthful state.
    return ApprovalStep(PAUSE)
