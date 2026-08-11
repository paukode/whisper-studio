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

category_modes (optional, in permissions.json):
  {"write": "acceptEdits", "cli": "default", ...} — per-category override of
  the global mode above, keyed by the ApprovalSpec categories declared in
  server/approval/bootstrap.py (write, delete, cli, worktree, preview,
  github, github-destructive, ...) plus the policy-only "workflow" category
  (new workflow-script launches; resolved by
  server/workflows/launch_policy.py). A category with no entry (including a
  legacy permissions.json with no "category_modes" key at all) falls back
  to the global mode. The one exception is "github-destructive", which
  always asks no matter what a category mode says — see
  resolve_static_decision below.

"auto" mode's classifier can flip-flop over a long turn; record_classifier_verdict /
is_auto_mode_tripped / resume_auto_mode below implement a per-turn circuit breaker
that forces the effective decision back to "ask" once denials pile up (see their
docstrings for the exact thresholds). server.tool_executor consults it right before
calling the classifier.
"""

import copy
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
    "category_modes": {},
    # DEFAULT-OFF egress control for sandboxed commands (server/sandbox.py +
    # server/security/egress_policy.py). "permissive" == today's behavior:
    # no domain restriction, no proxy. See egress_policy.py for the tier
    # semantics and DEFAULT_NETWORK_POLICY (kept in sync with this literal
    # by hand — a module-level import either way would create a cycle).
    "network_policy": {
        "tier": "permissive",
        "custom_allowed_domains": [],
        "methods_only_safe": False,
    },
}

# ── Auto-mode circuit breaker ───────────────────────────────────────────────
# The "auto" mode classifier (server.auto_mode.classify_tool_call) only ever
# sees one tool call in isolation and can flip-flop across a long turn. This
# tracks its verdicts per session, scoped to a single turn, and trips the
# breaker when denials pile up — forcing the effective mode back to "ask"
# instead of continuing to consult the classifier.
#
# Two independent triggers, mirroring hooks/engine.py's simple counted-cap
# (MAX_STOP_BLOCKS_PER_TURN) but with a rolling window added since a
# classifier can also deny slowly across many calls without ever stringing
# 3 in a row:
#   - consecutive: 3 "confirm" verdicts in a row with no intervening
#     "allow" — auto mode has clearly stopped agreeing with the work.
#   - windowed: 5 "confirm" verdicts within the last 10 classifier calls
#     this turn — catches a slower drip of denials that never quite
#     strings 3 together.
AUTO_MODE_CONSECUTIVE_DENIAL_LIMIT = 3
AUTO_MODE_WINDOW_SIZE = 10
AUTO_MODE_WINDOW_DENIAL_LIMIT = 5

# session_id -> {"consecutive": int, "window": list[bool], "tripped": bool, "reason": str | None}
# In-memory, process-wide, keyed by session_id — same shape as
# server.chat.engine.pause.paused_sessions. Reset at the start of each new
# turn (server.chat.engine.runner calls reset_auto_mode_breaker when
# ctx.is_new_turn); a backend restart drops it, same as any other in-flight
# turn state.
_auto_mode_breaker: dict[str, dict] = {}


def _breaker_state(session_id: str) -> dict:
    return _auto_mode_breaker.setdefault(
        session_id, {"consecutive": 0, "window": [], "tripped": False, "reason": None}
    )


def reset_auto_mode_breaker(session_id: str) -> None:
    """Clear a session's classifier-denial tracking. Called at the start of
    a genuinely new turn (not a paused-approval continuation) so a breaker
    tripped last turn doesn't carry over."""
    _auto_mode_breaker.pop(session_id, None)


def is_auto_mode_tripped(session_id: str) -> bool:
    """True once the breaker has tripped for this session's current turn."""
    return _auto_mode_breaker.get(session_id, {}).get("tripped", False)


def record_classifier_verdict(session_id: str, allowed: bool) -> dict:
    """Fold one auto-mode classifier verdict into the session's rolling
    denial tracker. Returns the (possibly just-tripped) breaker state; the
    caller should emit a one-time notification exactly when
    ``state["tripped"]`` is True on return (it stays False on every prior
    call, so this is only "new" once)."""
    state = _breaker_state(session_id)
    if state["tripped"]:
        return state
    if allowed:
        state["consecutive"] = 0
    else:
        state["consecutive"] += 1
    state["window"].append(allowed)
    if len(state["window"]) > AUTO_MODE_WINDOW_SIZE:
        del state["window"][: len(state["window"]) - AUTO_MODE_WINDOW_SIZE]
    window_denials = sum(1 for a in state["window"] if not a)
    if state["consecutive"] >= AUTO_MODE_CONSECUTIVE_DENIAL_LIMIT:
        state["tripped"] = True
        state["reason"] = (
            f"Auto mode asked for confirmation {state['consecutive']} times in a row this turn."
        )
    elif window_denials >= AUTO_MODE_WINDOW_DENIAL_LIMIT:
        state["tripped"] = True
        state["reason"] = (
            f"Auto mode asked for confirmation {window_denials} times in the last "
            f"{len(state['window'])} tool calls this turn."
        )
    return state


def resume_auto_mode(session_id: str) -> bool:
    """User override: clear the trip so auto mode resumes for the rest of
    this turn. Also resets the running counts (not just the flag) so a
    fresh run of denials can still re-trip before the turn ends, instead of
    the stale count re-tripping on the very next call. Returns False if
    there was nothing tripped to resume."""
    state = _auto_mode_breaker.get(session_id)
    if state is None or not state["tripped"]:
        return False
    state["tripped"] = False
    state["reason"] = None
    state["consecutive"] = 0
    state["window"] = []
    return True


def load_permissions() -> dict:
    # DEFAULTS holds mutable containers ("rules": [], "category_modes": {}).
    # Deep-copy before merging/returning so a caller that mutates the result
    # in place (e.g. add_rule's data["rules"].append(...)) never reaches back
    # into the module-level DEFAULTS singleton and corrupts it for the rest
    # of the process.
    defaults = copy.deepcopy(DEFAULTS)
    try:
        with open(PERMISSIONS_PATH) as f:
            data = json.load(f)
        return {**defaults, **data}
    except Exception:
        return defaults


def save_permissions(data: dict):
    os.makedirs(os.path.dirname(PERMISSIONS_PATH), exist_ok=True)
    with open(PERMISSIONS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_mode() -> str:
    """Return the current permission mode."""
    return load_permissions().get("mode", MODE_DEFAULT)


def _match_rule(tool_name: str, tool_input: dict, rules: list) -> tuple[int | None, dict | None]:
    """Find the first rule in `rules` that matches (tool_name, tool_input).
    Returns (index, rule) or (None, None). Shared by evaluate_rules and the
    /rules/test dry-run endpoint so both use the exact same matching logic."""
    input_str = _tool_input_str(tool_name, tool_input)
    for index, rule in enumerate(rules):
        if rule.get("tool") == tool_name or rule.get("tool") == "*":
            if "prefix" in rule:
                if input_str.startswith(rule["prefix"]):
                    return index, rule
            else:
                pattern = rule.get("pattern", "*")
                if pattern == "*" or fnmatch.fnmatch(input_str, pattern):
                    return index, rule
    return None, None


def _validate_network_policy(np: object) -> str | None:
    """Returns an error string if `np` isn't a valid (partial) network_policy
    body, or None if it's fine to merge onto the saved config."""
    from server.security.egress_policy import VALID_TIERS

    if not isinstance(np, dict):
        return "network_policy must be an object"
    if "tier" in np and np["tier"] not in VALID_TIERS:
        return f"network_policy.tier must be one of: {', '.join(sorted(VALID_TIERS))}"
    if "custom_allowed_domains" in np:
        domains = np["custom_allowed_domains"]
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            return "network_policy.custom_allowed_domains must be a list of strings"
    if "methods_only_safe" in np and not isinstance(np["methods_only_safe"], bool):
        return "network_policy.methods_only_safe must be a boolean"
    return None


def evaluate_rules(tool_name: str, tool_input: dict, rules: list | None = None) -> str | None:
    """Evaluate custom permission rules (permissions.json's `rules` array) against
    a tool call. Returns the first matching rule's action, or None if no rule matches."""
    if rules is None:
        rules = load_permissions().get("rules", [])
    _, rule = _match_rule(tool_name, tool_input, rules)
    return rule.get("action", "ask") if rule is not None else None


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

    Evaluation order: github-destructive (absolute) → category mode override →
    bypassPermissions (absolute) → trusted skill script → session approvals
    ("yes/no for all this session") → explicit custom rules → dontAsk →
    acceptEdits (write category only) → auto (defer) → ask.
    """
    # Destructive GitHub mutations (repo/ref delete, PR merge, archive/rename,
    # API DELETE) ALWAYS require an explicit human approval — no bypass mode,
    # autopilot, trusted-skill, blanket session approval, or category mode
    # override may cover them, given the irreversibility and remote blast
    # radius. Checked before everything, including the category mode lookup
    # right below.
    if category == "github-destructive":
        return "ask"

    # MCP tool calls: a dedicated tier on top of (not instead of) everything
    # below. A server/tool marked `approval_mode: "approve"` in
    # mcp_servers.json (server.mcp.MCPManager.get_tool_approval_tier_for_
    # tool_name) is a hard floor — like github-destructive, no bypass mode,
    # trusted-skill, or blanket session approval may skip it, because the
    # user explicitly marked that server as one they don't fully trust.
    # Every other tier ("auto", "prompt", "writes") falls straight through to
    # the standard resolution below unchanged — the per-server config only
    # ever ADDS a gate (server.mcp.MCPManager.call_tool decides whether to
    # ask at all); it never removes the user's ability to configure the
    # ordinary mode/session/rule pipeline for MCP tools like any other tool.
    if category == "mcp":
        from server.mcp import mcp_manager

        if mcp_manager.get_tool_approval_tier_for_tool_name(tool_name) == "approve":
            return "ask"

    # Per-category default overrides the global mode for everything that
    # follows (including the bypassPermissions check right below) — that's
    # the point: e.g. "always confirm deletes" can hold even when the global
    # mode is more permissive. A legacy permissions.json with no
    # "category_modes" key at all, or no entry for this category, behaves as
    # if no override exists and `mode` is left as the caller passed it.
    category_mode = (load_permissions().get("category_modes") or {}).get(category)
    if category_mode in VALID_MODES:
        mode = category_mode

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
    from server.approval.registry import all_categories

    data = load_permissions()
    # "categories" is computed from the live ApprovalSpec registry, not
    # persisted — it just tells the frontend which category_modes rows to
    # render (server/approval/bootstrap.py is the source of truth).
    categories = all_categories()
    # "workflow" is a policy-only category: new workflow-script launches
    # consult it via server/workflows/launch_policy.py instead of the
    # ApprovalSpec registry, but users configure it like any other row.
    if "workflow" not in categories:
        categories.append("workflow")
    return {**data, "categories": categories}


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
    if "category_modes" in body:
        category_modes = body["category_modes"]
        if not isinstance(category_modes, dict):
            return Response(
                content=json.dumps({"error": "category_modes must be an object"}),
                status_code=400,
                media_type="application/json",
            )
        cleaned: dict = {}
        for cat, cat_mode in category_modes.items():
            if not cat_mode:
                continue  # falsy/empty = "use the global mode", nothing to store
            if cat_mode not in VALID_MODES:
                return Response(
                    content=json.dumps(
                        {
                            "error": (
                                f"category_modes.{cat} must be one of: "
                                f"{', '.join(sorted(VALID_MODES))}"
                            )
                        }
                    ),
                    status_code=400,
                    media_type="application/json",
                )
            cleaned[cat] = cat_mode
        data["category_modes"] = cleaned
    if "network_policy" in body:
        error = _validate_network_policy(body["network_policy"])
        if error:
            return Response(
                content=json.dumps({"error": error}), status_code=400, media_type="application/json"
            )
        existing = data.get("network_policy") or {}
        data["network_policy"] = {**existing, **body["network_policy"]}
    save_permissions(data)
    warnings = detect_shadowed_rules(data["rules"])
    return {
        "updated": True,
        "mode": data["mode"],
        "rules": data["rules"],
        "category_modes": data.get("category_modes", {}),
        "warnings": warnings,
    }


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
    # Purely additive audit-trail metadata — never consulted by evaluate_rules
    # or resolve_static_decision, just persisted and returned so the UI can
    # show why a rule exists.
    justification = body.get("justification")
    if justification:
        rule["justification"] = justification
    data = load_permissions()
    data["rules"].append(rule)
    save_permissions(data)
    warnings = detect_shadowed_rules(data["rules"])
    return {"added": True, "rules": data["rules"], "warnings": warnings}


@router.post("/rules/test")
async def test_rule(request: Request):
    """Dry-run rule matching for a sample tool/input pair — the rule editor's
    "Test this rule" button. Mirrors server.hooks.routes.test_hook's shape:
    run the same evaluation the live path uses and report the decision.

    Matches against the persisted rule set by default. Pass "rules" (a list)
    to test a draft list in isolation — e.g. the one rule you're about to
    save, before you've saved it."""
    body = await request.json()
    tool = body.get("tool", "")
    if not tool:
        return Response(
            content=json.dumps({"error": "tool is required"}),
            status_code=400,
            media_type="application/json",
        )
    tool_input = body.get("input") or {}
    rules = body.get("rules")
    if rules is None:
        rules = load_permissions().get("rules", [])
    index, rule = _match_rule(tool, tool_input, rules)
    decision = rule.get("action", "ask") if rule is not None else None
    return {"decision": decision, "matched_index": index, "matched_rule": rule}


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


@router.post("/auto-mode/resume")
async def resume_auto_mode_route(request: Request):
    """One-click override for the auto-mode circuit breaker (mirrors Codex's
    /approve-after-circuit-break): the user asks auto mode to resume
    classifying for the rest of the current turn instead of asking for
    every remaining tool call. Does not touch the persisted permission
    mode — the breaker is turn-scoped, in-memory state."""
    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return Response(
            content=json.dumps({"error": "session_id is required"}),
            status_code=400,
            media_type="application/json",
        )
    resumed = resume_auto_mode(session_id)
    return {"resumed": resumed}
