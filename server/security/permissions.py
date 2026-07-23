"""
Permission rule system for Whisper Studio.
Rules format:
  {"tool": "ws_write_file", "pattern": "*", "action": "ask"}
  {"tool": "ws_run_command", "pattern": "git *", "action": "allow"}
  {"tool": "ws_run_command", "prefix": "git ", "action": "allow"}
  {"tool": "ws_delete_file", "pattern": "*.log", "action": "deny"}

Actions: "allow" | "ask" | "deny"
Modes:
  "default"          — read-only tools auto-allow; write tools ask
  "auto"             — read-only auto-allow; write tools go through Haiku classifier
  "plan"             — read-only auto-allow; ws_write_file/ws_create_file/ws_edit_file/
                        ws_delete_file/ws_run_command/ws_merge_worktree are hard-blocked
                        pre-dispatch (see _PLAN_MODE_BLOCKED in tool_executor.py); other
                        action tools (terminal_run, git_*, aws_cli, run_python, worktree
                        enter/exit) still go through the normal approval card
  "acceptEdits"      — ws_write_file/ws_create_file auto-allow; delete+commands ask
  "bypassPermissions"— everything allowed, no prompts
  "dontAsk"          — read-only auto-allow; write tools auto-deny silently
"""

import fnmatch
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

DATA_DIR = data_root()
PERMISSIONS_PATH = os.path.join(DATA_DIR, "permissions.json")

# ── Mode constants ─────────────────────────────────────────────────────────────
MODE_DEFAULT = "default"
MODE_AUTO = "auto"
MODE_PLAN = "plan"
MODE_ACCEPT_EDITS = "acceptEdits"
MODE_BYPASS = "bypassPermissions"
MODE_DONT_ASK = "dontAsk"

VALID_MODES = {MODE_DEFAULT, MODE_AUTO, MODE_PLAN, MODE_ACCEPT_EDITS, MODE_BYPASS, MODE_DONT_ASK}

DEFAULTS = {
    "mode": "default",
    "rules": [],
}


def load_permissions() -> dict:
    try:
        with open(PERMISSIONS_PATH) as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    except Exception:
        return dict(DEFAULTS)


def save_permissions(data: dict):
    os.makedirs(os.path.dirname(PERMISSIONS_PATH), exist_ok=True)
    with open(PERMISSIONS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_mode() -> str:
    """Return the current permission mode."""
    return load_permissions().get("mode", MODE_DEFAULT)


def evaluate_rules(tool_name: str, tool_input: dict, rules: list | None = None) -> str | None:
    """Evaluate custom permission rules (permissions.json's `rules` array) against
    a tool call. Returns the first matching rule's action, or None if no rule matches."""
    if rules is None:
        rules = load_permissions().get("rules", [])
    input_str = _tool_input_str(tool_name, tool_input)
    for rule in rules:
        if rule.get("tool") == tool_name or rule.get("tool") == "*":
            if "prefix" in rule:
                if input_str.startswith(rule["prefix"]):
                    return rule.get("action", "ask")
            else:
                pattern = rule.get("pattern", "*")
                if pattern == "*" or fnmatch.fnmatch(input_str, pattern):
                    return rule.get("action", "ask")
    return None


def resolve_static_decision(
    tool_name: str,
    tool_input: dict,
    category: str,
    session_approvals: dict,
    mode: str,
    auto_allow_trusted: bool = False,
) -> str | None:
    """Resolve an approval-gated tool call's decision without the async auto-mode
    classifier.

    Returns "allow" | "ask" | "deny", or None when mode is "auto" and nothing
    else resolved it — the caller should fall back to the classifier in that case.

    Evaluation order: bypassPermissions (absolute) → trusted skill script →
    session approvals ("yes/no for all this session") → explicit custom rules →
    dontAsk → acceptEdits (write category only) → auto (defer) → ask.
    """
    # Destructive GitHub mutations (repo/ref delete, PR merge, archive/rename,
    # API DELETE) ALWAYS require an explicit human approval — no bypass mode,
    # autopilot, trusted-skill, or blanket session approval may cover them, given
    # the irreversibility and remote blast radius. Checked before everything.
    if category == "github-destructive":
        return "ask"
    if mode == MODE_BYPASS:
        return "allow"
    if auto_allow_trusted:
        return "allow"
    if session_approvals.get(category) == "allow":
        return "allow"
    if session_approvals.get(category) == "deny":
        return "deny"

    rule_decision = evaluate_rules(tool_name, tool_input)
    if rule_decision is not None:
        return rule_decision

    if mode == MODE_DONT_ASK:
        return "deny"
    if mode == MODE_ACCEPT_EDITS and category == "write":
        return "allow"
    if mode == MODE_AUTO:
        return None

    return "ask"


# Field to match rule patterns/prefixes against, per tool. Tools not listed
# here fall back to matching the JSON-encoded input (mainly useful with a
# wildcard "*" pattern).
_RULE_MATCH_FIELDS = {
    "ws_write_file": "path",
    "ws_create_file": "path",
    "ws_delete_file": "path",
    "ws_read_file": "path",
    "ws_run_command": "command",
    "terminal_run": "command",
    "run_python": "code",
    "aws_cli": "command",
    "git_push": "branch",
    "git_checkout": "branch",
    "git_merge": "branch",
    "git_create_branch": "name",
    "git_delete_branch": "branch",
    "git_add_commit": "message",
    "enter_worktree": "name",
}


def _tool_input_str(tool_name: str, tool_input: dict) -> str:
    """Build a matchable string from tool input for pattern matching."""
    field = _RULE_MATCH_FIELDS.get(tool_name)
    if field:
        return str(tool_input.get(field, ""))
    return json.dumps(tool_input)


# --- API Routes ---


@router.get("")
async def get_permissions():
    return load_permissions()


@router.put("")
async def update_permissions(request: Request):
    from server.security.shadowed_rules import detect_shadowed_rules

    body = await request.json()
    data = load_permissions()
    if "mode" in body:
        mode = body["mode"]
        if mode not in VALID_MODES:
            return Response(
                content=json.dumps(
                    {"error": f"mode must be one of: {', '.join(sorted(VALID_MODES))}"}
                ),
                status_code=400,
                media_type="application/json",
            )
        data["mode"] = mode
    if "rules" in body:
        data["rules"] = body["rules"]
    save_permissions(data)
    warnings = detect_shadowed_rules(data["rules"])
    return {"updated": True, "mode": data["mode"], "rules": data["rules"], "warnings": warnings}


@router.post("/rules")
async def add_rule(request: Request):
    from server.security.shadowed_rules import detect_shadowed_rules

    body = await request.json()
    tool = body.get("tool", "")
    action = body.get("action", "ask")
    if not tool or action not in ("allow", "ask", "deny"):
        return Response(
            content=json.dumps({"error": "Invalid rule. action must be allow/ask/deny"}),
            status_code=400,
            media_type="application/json",
        )
    rule: dict = {"tool": tool, "action": action}
    if "prefix" in body:
        rule["prefix"] = body["prefix"]
    else:
        rule["pattern"] = body.get("pattern", "*")
    data = load_permissions()
    data["rules"].append(rule)
    save_permissions(data)
    warnings = detect_shadowed_rules(data["rules"])
    return {"added": True, "rules": data["rules"], "warnings": warnings}


@router.delete("/rules/{index}")
async def delete_rule(index: int):
    from server.security.shadowed_rules import detect_shadowed_rules

    data = load_permissions()
    rules = data.get("rules", [])
    if index < 0 or index >= len(rules):
        return Response(
            content=json.dumps({"error": "Rule not found"}),
            status_code=404,
            media_type="application/json",
        )
    deleted = rules.pop(index)
    data["rules"] = rules
    save_permissions(data)
    warnings = detect_shadowed_rules(rules)
    return {"deleted": deleted, "rules": rules, "warnings": warnings}


@router.put("/mode")
async def set_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", MODE_DEFAULT)
    if mode not in VALID_MODES:
        return Response(
            content=json.dumps({"error": f"mode must be one of: {', '.join(sorted(VALID_MODES))}"}),
            status_code=400,
            media_type="application/json",
        )
    data = load_permissions()
    data["mode"] = mode
    save_permissions(data)
    return {"mode": mode}
