"""ApprovalSpec dataclass — what a tool declares about its approval needs."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("whisper-studio")

PreviewKind = Literal["diff", "command", "list", "text"]
RiskHint = Literal["low", "medium", "high"]

# Payload key holding the workspace root a workspace-bound card was raised in.
WORKSPACE_ROOT_KEY = "workspace_root"


@dataclass
class ApprovalOutcome:
    """What the executor returns to the frontend after the user clicks Yes."""

    ok: bool
    error: str | None = None
    output: str | None = None
    # The user's Stop ended the action before it finished (an approved
    # ws_run_command; see server/approval/executors.py).
    stopped: bool = False


def refuse_if_agent(payload: dict, what: str = "This action") -> ApprovalOutcome | None:
    """Refuse a mutation that a subagent auto-approved with no human present.

    The subagent path (server/agents/runtime.py) auto-executes `[WS_APPROVAL]`
    actions unconditionally — category / risk_hint / session approvals are NOT
    consulted there. `_execute_ws_approval_inline(agent=True)` stamps `__agent__`
    onto such payloads. High-blast-radius executors (GitHub mutations, and any
    irreversible/remote action) call this first and bail out if the stamp is
    present, so an unattended agent cannot merge/close/delete on its own.

    Returns a refusal ApprovalOutcome when the payload is agent-originated, else
    None (caller proceeds)."""
    if payload.get("__agent__"):
        return ApprovalOutcome(
            ok=False,
            error=(
                f"{what} is not permitted from an unattended subagent. Ask the "
                "top-level session, where a human can approve it, to run this."
            ),
        )
    return None


def refuse_if_agent_rm(payload: dict, command_field: str = "command") -> ApprovalOutcome | None:
    """Refuse rm (and its close cousins — see is_rm_command) from an unattended
    subagent, unconditionally.

    Deleting files is exactly the kind of irreversible action a human must see
    before it happens; the unattended auto-approve path (agent=True) skips
    every other gate (category, session rules, the classifier) by design, so
    this is the only check standing between an agent's own `rm` and actually
    running it. Belt-and-suspenders with the interactive-mode guardrail in
    server/tool_executor.py, which forces a real approval prompt for rm even
    when a human WOULD normally have pre-approved the whole "cli" category.
    """
    if not payload.get("__agent__"):
        return None
    from server.security.command_validator import is_rm_command

    if not is_rm_command(payload.get(command_field) or ""):
        return None
    return ApprovalOutcome(
        ok=False,
        error=(
            "rm (or an equivalent delete) is never run from an unattended "
            "subagent or workflow, no matter the permission mode. Ask the "
            "top-level session, where a human can approve it, to run this."
        ),
    )


# Executor signature: takes the approval payload dict, returns an outcome.
# Sync functions are wrapped at registration time so the registry exposes
# a uniform Awaitable interface to /api/approval/execute.
Executor = Callable[[dict], Awaitable[ApprovalOutcome]]


@dataclass
class ApprovalSpec:
    """Declarative description of an approval-required tool.

    `category` is the bucket session-memory uses ("Yes for all writes" stores
    `allow` here). Extensible — categories are discovered by walking the
    registry, not pinned to a literal union.

    `preview` picks which of the four frontend renderers shows the action:
    diff (file content changes), command (shell), list (multi-file ops),
    text (free-form summary).

    `summary` produces the one-line human-readable label. Pass a string for
    constant labels or a callable that takes the tool input dict for
    dynamic ones.

    `executor` is the function that actually performs the action on Yes.
    Single hub — the frontend never knows which endpoint runs the work.

    `render_command` is an optional callable that produces the literal
    command/preview-body string. When set, `build_payload` injects it as
    payload.command so the frontend's CommandPreview renders it verbatim.
    Lets the `summary` field stay short (header) while the body still
    shows the full action (e.g. `git commit -m 'long message…'`).

    `on_approved_by_human` is an optional callable run with the payload just
    before the executor when a person approved the card (its Yes button, or a
    spoken yes), and never when a mode, a session approval, the classifier or
    an unattended agent let the action through. See execute_approved_by_human.

    `workspace_bound` marks an action whose executor resolves against the
    workspace connected when the user clicks Yes (a workspace-relative path, a
    command run in the workspace, a git or gh call in its repo). Pass True, or
    a predicate over the payload when it depends on the call (a terminal run
    with no explicit cwd). The gate stamps such a card with the root it was
    raised in (`workspace_root`), and a Yes after that workspace was
    disconnected or replaced is refused instead of acting on the new one.
    """

    category: str
    preview: PreviewKind
    summary: str | Callable[[dict], str]
    executor: Executor
    risk_hint: RiskHint | None = None
    # Fields of the tool input that should be forwarded to the frontend as
    # part of the approval payload. The frontend's preview renderer reads
    # them by name. Defaults to all input fields if unset.
    payload_fields: list[str] | None = field(default=None)
    render_command: Callable[[dict], str] | None = None
    on_approved_by_human: Callable[[dict], None] | None = None
    workspace_bound: bool | Callable[[dict], bool] = False

    def binds_workspace(self, payload: dict) -> bool:
        if callable(self.workspace_bound):
            return bool(self.workspace_bound(payload))
        return bool(self.workspace_bound)

    def render_summary(self, tool_input: dict) -> str:
        if callable(self.summary):
            try:
                return self.summary(tool_input)
            except Exception as e:  # noqa: BLE001 - summary fns are user-defined
                return f"({self.category} action — summary error: {e})"
        return self.summary

    def build_payload(self, tool_input: dict) -> dict:
        if self.payload_fields is None:
            payload = dict(tool_input)
        else:
            payload = {k: tool_input.get(k) for k in self.payload_fields if k in tool_input}
        if self.render_command and "command" not in payload:
            try:
                payload["command"] = self.render_command(tool_input)
            except Exception:  # noqa: BLE001
                pass
        # Outside payload_fields on purpose: the gate stamps it on every card
        # of a workspace-bound action, and it must survive the whitelist.
        if WORKSPACE_ROOT_KEY in tool_input:
            payload[WORKSPACE_ROOT_KEY] = tool_input[WORKSPACE_ROOT_KEY]
        return payload


def stale_workspace_refusal(
    spec: ApprovalSpec, payload: dict, *, subject: str = "This approval"
) -> ApprovalOutcome | None:
    """Refuse a workspace-bound card whose workspace is no longer connected.

    A card waits for the user, and the user may disconnect the workspace (or
    connect another) before clicking Yes. Its payload holds only a
    workspace-relative path or a command, so running it then would write,
    delete or run in whatever is connected now: a card raised in A and
    approved after connecting B deleted B's file of the same name. The card
    carries the root it was raised in (stamped by the gate), and a mismatch
    changes nothing. A payload with no stamp was not raised by the gate and
    makes no claim, so it runs as before.
    """
    if WORKSPACE_ROOT_KEY not in payload or not spec.binds_workspace(payload):
        return None
    from server.workspace.state import load_workspace_config

    raised_in = payload.get(WORKSPACE_ROOT_KEY) or None
    current = load_workspace_config().get("path") or None
    if _same_root(raised_in, current):
        return None
    if raised_in is None:
        why = f"it was raised with no workspace connected, and {current} is connected now"
    elif current is None:
        why = f"it was for {raised_in}, which is no longer connected"
    else:
        why = f"it was for {raised_in}, and {current} is connected now"
    return ApprovalOutcome(
        ok=False,
        error=f"{subject} is out of date: {why}. Nothing was changed.",
    )


def turn_workspace_refusal(spec: ApprovalSpec, payload: dict) -> str | None:
    """Why the running turn may not run this workspace-bound action now, or None.

    The gate (server/tool_executor.py) asks before it raises a card and again
    right before it runs an action no card waits for (a session approval, the
    auto-mode classifier, an unattended agent), each time under the turn's
    workspace latch. Refused when the user let go of the turn's workspace
    since it began (server.workspace.state.workspace_lost_message), or when
    the root stamped on the payload when the tool ran is no longer the
    connected one. A pinned run (a per-task override) resolves against its
    own root, so neither applies to it.
    """
    if not spec.binds_workspace(payload):
        return None
    from server.workspace.state import get_workspace_override, workspace_lost_message

    if get_workspace_override():
        return None
    lost = workspace_lost_message()
    if lost:
        return lost
    stale = stale_workspace_refusal(spec, payload, subject="This action")
    return f"Error: {stale.error}" if stale else None


def _same_root(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return not a and not b
    return os.path.realpath(a) == os.path.realpath(b)


async def execute_approved_by_human(spec: ApprovalSpec, payload: dict) -> ApprovalOutcome:
    """Run an action a person approved: the card's Yes (POST /api/approval/execute)
    or a spoken yes in voice mode. The only entry point that runs the spec's
    ``on_approved_by_human`` hook, so what a person approved can be remembered
    (a trusted launch config) without an automatic path ever doing the same.
    A failing hook is logged and does not stop the approved action.

    A workspace-bound card approved after its workspace went away is refused
    first (stale_workspace_refusal), before the hook or the executor runs."""
    stale = stale_workspace_refusal(spec, payload)
    if stale is not None:
        log.info("approval: refused an out-of-date card: %s", stale.error)
        return stale
    hook = getattr(spec, "on_approved_by_human", None)
    if hook is not None:
        try:
            hook(payload)
        except Exception:  # noqa: BLE001 - the approved action still runs
            log.exception("approval: on_approved_by_human hook failed")
    return await spec.executor(payload)
