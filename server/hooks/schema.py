"""Hook definitions, outcomes, matcher semantics, and config normalization.

Config schema v2 (nested by event):
    {"version": 2, "hooks": {
        "PreToolUse": [{"id": "h_ab12", "matcher": "ws_write_file|ws_edit_file",
                        "command": "...", "timeout": 10, "enabled": true,
                        "on_error": "ignore"}],
        "Stop": [...], ...}}

v1 (the legacy flat list, {event,tool,command}) is transparently normalized on
load so existing user hooks keep working. Matcher: exact name, "*", pipe-list
("a|b"), or "/regex/". Events without a tool (Stop, SessionStart) ignore it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)
# TurnEnd is a config alias of Stop (spec names both; one wire point).
_EVENT_ALIASES = {"TurnEnd": "Stop"}

STDIN_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 60


@dataclass
class HookDef:
    event: str
    command: str
    matcher: str = "*"
    timeout: int = DEFAULT_TIMEOUT
    enabled: bool = True
    on_error: str = "ignore"  # "ignore" | "block"
    id: str = ""
    source: str = "user"  # "user" | "project" | "plugin"

    def clamp(self) -> HookDef:
        self.timeout = max(1, min(int(self.timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        if self.on_error not in ("ignore", "block"):
            self.on_error = "ignore"
        if not self.id:
            self.id = "h_" + uuid.uuid4().hex[:8]
        return self


@dataclass
class HookOutcome:
    """Merged result of every hook that ran for one event."""

    decision: str = "allow"  # "allow" | "deny"
    reason: str = ""
    updated_input: dict | None = None
    contexts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Structured findings from a plugin deny (e.g. security_checks); drives the
    # existing security_blocked UI frame. None for a plain shell/project deny.
    findings: object | None = None

    @property
    def blocked(self) -> bool:
        return self.decision == "deny"


def canonical_event(event: str) -> str:
    return _EVENT_ALIASES.get(event, event)


def matches(matcher: str, tool_name: str | None) -> bool:
    """Does a hook's matcher select this tool? Events without a tool always
    match (tool_name is None)."""
    if tool_name is None:
        return True
    m = (matcher or "*").strip()
    if m in ("", "*"):
        return True
    if m.startswith("/") and m.endswith("/") and len(m) > 1:
        try:
            return re.search(m[1:-1], tool_name) is not None
        except re.error:
            return False
    if "|" in m:
        return tool_name in {p.strip() for p in m.split("|") if p.strip()}
    return tool_name == m


def _load_v1_list(items: list, by_event: dict[str, list[HookDef]], source: str) -> None:
    """v1 flat list: [{event, tool, command}]. The old save_hooks wrote this
    either bare or wrapped as {"hooks": [...]}, so both shapes route here."""
    for item in items:
        if not isinstance(item, dict):
            continue
        ev = canonical_event(item.get("event", ""))
        if ev not in by_event:
            continue
        cmd = (item.get("command") or "").strip()
        if not cmd:
            continue
        by_event[ev].append(
            HookDef(event=ev, command=cmd, matcher=item.get("tool", "*"), source=source).clamp()
        )


def normalize_config(raw: dict | list | None, *, source: str) -> dict[str, list[HookDef]]:
    """Return {event: [HookDef]} from either the v2 nested dict or the v1 flat
    list (bare or {"hooks": [...]}-wrapped). Unknown events are dropped; aliases
    are folded."""
    by_event: dict[str, list[HookDef]] = {e: [] for e in HOOK_EVENTS}
    if not raw:
        return by_event

    # v1 flat list, top-level.
    if isinstance(raw, list):
        _load_v1_list(raw, by_event, source)
        return by_event

    hooks = raw.get("hooks", raw) if isinstance(raw, dict) else {}
    # v1 flat list wrapped as {"hooks": [...]} (the legacy on-disk format).
    if isinstance(hooks, list):
        _load_v1_list(hooks, by_event, source)
        return by_event
    # v2 nested: {"hooks": {event: [ {matcher, command, ...} ]}}
    if not isinstance(hooks, dict):
        return by_event
    for ev_raw, defs in hooks.items():
        ev = canonical_event(ev_raw)
        if ev not in by_event or not isinstance(defs, list):
            continue
        for d in defs:
            if not isinstance(d, dict):
                continue
            cmd = (d.get("command") or "").strip()
            if not cmd:
                continue
            by_event[ev].append(
                HookDef(
                    event=ev,
                    command=cmd,
                    matcher=d.get("matcher", d.get("tool", "*")),
                    timeout=d.get("timeout", DEFAULT_TIMEOUT),
                    enabled=d.get("enabled", True),
                    on_error=d.get("on_error", "ignore"),
                    id=d.get("id", ""),
                    source=source,
                ).clamp()
            )
    return by_event


def serialize_v2(by_event: dict[str, list[HookDef]]) -> dict:
    """Persist the user layer as v2 (with generated ids)."""
    out: dict[str, list[dict]] = {}
    for ev, defs in by_event.items():
        rows = []
        for d in defs:
            rows.append(
                {
                    "id": d.id,
                    "matcher": d.matcher,
                    "command": d.command,
                    "timeout": d.timeout,
                    "enabled": d.enabled,
                    "on_error": d.on_error,
                }
            )
        if rows:
            out[ev] = rows
    return {"version": 2, "hooks": out}


def build_stdin_payload(
    event: str,
    *,
    session_id: str = "",
    workspace: str = "",
    tool_name: str | None = None,
    tool_input: dict | None = None,
    tool_output: str | None = None,
    stop_hook_active: bool = False,
    round_num: int | None = None,
    model_id: str = "",
) -> dict:
    payload: dict = {
        "schema": STDIN_SCHEMA_VERSION,
        "event": event,
        "session_id": session_id,
        "workspace": workspace,
        "model_id": model_id,
    }
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if tool_input is not None:
        payload["tool_input"] = tool_input
    if tool_output is not None:
        payload["tool_output"] = tool_output
    if event in ("Stop",):
        payload["stop_hook_active"] = stop_hook_active
    if round_num is not None:
        payload["round"] = round_num
    return payload
