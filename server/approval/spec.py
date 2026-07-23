"""ApprovalSpec dataclass — what a tool declares about its approval needs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

PreviewKind = Literal["diff", "command", "list", "text"]
RiskHint = Literal["low", "medium", "high"]


@dataclass
class ApprovalOutcome:
    """What the executor returns to the frontend after the user clicks Yes."""

    ok: bool
    error: str | None = None
    output: str | None = None


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
        return payload
