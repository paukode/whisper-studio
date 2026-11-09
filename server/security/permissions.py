"""
Permission modes for Whisper Studio.

The permission mode is the only setting a person changes (the composer's Mode
menu). permissions.json holds that one value: {"mode": "default"}.

Decisions: "allow" | "ask" | "deny"
Modes:
  "default"            read-only tools auto-allow; write tools ask
  "auto"               read-only auto-allow; write tools go through Haiku classifier
  "plan"               read-only auto-allow; ws_write_file/ws_create_file/ws_edit_file/
                        ws_delete_file/ws_run_command/ws_merge_worktree are hard-blocked
                        pre-dispatch (see _PLAN_MODE_BLOCKED in tool_executor.py); memory
                        writes run with no card, a rules_write raises a card; other
                        action tools (terminal_run, git_*, aws_cli, run_python, worktree
                        enter/exit) still go through the normal approval card
  "acceptEdits"        ws_write_file/ws_create_file auto-allow; delete+commands ask
  "bypassPermissions": everything allowed, no prompts
  "dontAsk"            read-only auto-allow; write tools auto-deny silently

The ApprovalSpec categories (server/approval/bootstrap.py: write, delete,
cli, worktree, preview, github, github-destructive, ...) only key the session
approvals ("Yes, all" / "Block all" for one session). "github-destructive"
always asks, whatever the mode or a blanket session approval says, see
resolve_static_decision below. A command input that deletes outside the
connected workspace and the app's own folders
(server/security/delete_scope.py) asks in every mode but bypassPermissions.

The permission contract behind the order below (the user's decision of
2026-11-02): bypassPermissions works like Claude Code's bypass and shows no
card anywhere on the machine, with two exceptions that keep asking: a
destructive remote GitHub action and a tool of an MCP server marked
``approval_mode: "approve"``. The rm -rf / hard block
(server/security/command_validator.py) refuses in every mode. In every other
mode a change outside the workspace asks (a save to a path outside it, reach
into a folder outside it, a command that deletes outside it, a command asking
to write outside the sandbox); reading never asks anywhere on the machine;
the app's own folder (~/.whisper) is written without asking in every mode but
plan, and a delete there follows the mode, silent in bypass only. The app's own grant and config files in it
(server.infrastructure.paths.app_config_paths / app_config_dirs) are the
exception: a write, save or delete of one follows the permission mode instead
of the no-card allow, per the user's table (bypassPermissions allows, dontAsk
denies, auto lets the classifier decide, every other mode asks), and an
unattended run with no person to ask is refused (server/tool_approvals.py).

A preview_start that names a .whisper/launch.json config whose command a person
has approved once starts in every mode (server/preview/start_policy.py); a new
or changed config, and an ad-hoc preview command, follow the mode.

"auto" mode's classifier can flip-flop over a long turn; record_classifier_verdict /
is_auto_mode_tripped / resume_auto_mode below implement a per-turn circuit breaker
that forces the effective decision back to "ask" once denials pile up (see their
docstrings for the exact thresholds). server.tool_executor consults it right before
calling the classifier.
"""

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

# Floors that bypassPermissions lifts (the user's decision of 2026-11-02:
# Bypass works like Claude Code's bypass). Every other floor keeps asking in
# every mode: github-destructive and mcp-approve.
BYPASS_LIFTED_FLOORS = frozenset({"rm", "sandbox-escalation", "visible-terminal"})


# ── Auto-mode circuit breaker ───────────────────────────────────────────────
# The "auto" mode classifier (server.auto_mode.classify_tool_call) only ever
# sees one tool call in isolation and can flip-flop across a long turn. This
# tracks its verdicts per session, scoped to a single turn, and trips the
# breaker when denials pile up, forcing the effective mode back to "ask"
# instead of continuing to consult the classifier.
#
# Two independent triggers: a counted cap, and a rolling window, since a
# classifier can also deny slowly across many calls without ever stringing
# 3 in a row:
#   - consecutive: 3 "confirm" verdicts in a row with no intervening
#     "allow", auto mode has clearly stopped agreeing with the work.
#   - windowed: 5 "confirm" verdicts within the last 10 classifier calls
#     this turn, catches a slower drip of denials that never quite
#     strings 3 together.
AUTO_MODE_CONSECUTIVE_DENIAL_LIMIT = 3
AUTO_MODE_WINDOW_SIZE = 10
AUTO_MODE_WINDOW_DENIAL_LIMIT = 5

# session_id -> {"consecutive": int, "window": list[bool], "tripped": bool, "reason": str | None}
# In-memory, process-wide, keyed by session_id, same shape as
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
    """The saved permission settings: {"mode": ...}, and nothing else.

    Only the mode is read, so a key an older version wrote is ignored now and
    gone after the next save. A mode that is not one of VALID_MODES reads as
    the default (Asks first). A file that cannot be read or parsed raises:
    reading it as the default would quietly change the mode the user chose.
    """
    try:
        with open(PERMISSIONS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"mode": MODE_DEFAULT}
    except ValueError as e:
        raise PermissionsFileError(f"{PERMISSIONS_PATH} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise PermissionsFileError(f"{PERMISSIONS_PATH} does not hold a JSON object.")
    mode = data.get("mode")
    return {"mode": mode if mode in VALID_MODES else MODE_DEFAULT}


class PermissionsFileError(ValueError):
    """permissions.json exists but cannot be used."""


def save_mode(mode: str) -> None:
    """Write the permission mode. The file holds only the mode."""
    os.makedirs(os.path.dirname(PERMISSIONS_PATH), exist_ok=True)
    tmp = PERMISSIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mode": mode}, f, indent=2)
    os.replace(tmp, PERMISSIONS_PATH)


def get_mode() -> str:
    """Return the current permission mode."""
    return load_permissions()["mode"]


def approval_floor(
    tool_name: str, tool_input: dict, category: str, mode: str | None = None
) -> str | None:
    """The hard floor this call hits, or None.

    A floor that applies always asks a human: no trusted-skill flag, session
    approval ("Yes, all" / "Block") or mode default decides it. Returns which floor ("github-destructive", "rm",
    "sandbox-escalation", "visible-terminal", "mcp-approve"). Any floor marks
    the approval card's request ``always_asks``, so the card never offers a
    remember-for-session choice that would then be ignored. One floor is
    stricter in dontAsk: resolve_static_decision denies a github-destructive
    call there, since that mode never shows a card.

    ``mode`` is the session's permission mode. In bypassPermissions the
    floors in BYPASS_LIFTED_FLOORS do not apply (the user's decision of
    2026-11-02: Bypass works like Claude Code's bypass, so a delete outside
    the workspace, a sandbox escalation and a visible terminal run show no
    card). github-destructive and mcp-approve keep asking in every mode. ``mode``
    None (a caller that only asks whether any floor could apply) lifts
    nothing.
    """
    floor = _floor(tool_name, tool_input, category)
    if floor in BYPASS_LIFTED_FLOORS and mode == MODE_BYPASS:
        return None
    return floor


def bypass_now() -> bool:
    """Whether the app's permission mode is bypassPermissions right now.

    The executors of an unattended run (a subagent, a workflow script) read
    this: those runs carry no mode of their own, and they belong to the
    session the user set the mode for."""
    return get_mode() == MODE_BYPASS


def bypass_call(call_input: dict | None) -> bool:
    """Whether a dispatched tool call runs in bypassPermissions.

    An attended call carries server/tool_executor.py's stamp of the turn's
    mode (secret_reads.SECRET_READS_KEY). An unattended one (``__agent__``: a
    subagent, a workflow script) carries no mode of its own, so the app's
    mode decides (bypass_now)."""
    call_input = call_input or {}
    if call_input.get("__agent__"):
        return bypass_now()
    from server.security.secret_reads import stamped

    return stamped(call_input)


def _floor(tool_name: str, tool_input: dict, category: str) -> str | None:
    """The hard floor this call hits before the mode is applied, or None."""
    # Destructive GitHub mutations (repo/ref delete, PR merge, archive/rename,
    # API DELETE) ALWAYS require an explicit human approval, no bypass mode,
    # autopilot, trusted-skill or blanket session approval may cover them,
    # given the irreversibility and remote blast radius. Checked before
    # everything else in resolve_static_decision.
    if category == "github-destructive":
        return "github-destructive"

    # rm (and its close cousins, see delete_sites) reaching outside the
    # connected workspace and the app's own folders: outside bypassPermissions
    # no trusted-skill flag, blanket "allow all commands" session approval,
    # or dontAsk/acceptEdits default may let it
    # run without a real human clicking Approve. In bypassPermissions it runs
    # (approval_floor lifts it); the rm -rf / hard block in
    # server/security/command_validator.py still refuses there. A delete that
    # stays inside the workspace is an ordinary command and follows the mode;
    # the roots and the working
    # directory come from the call (_delete_roots), and an operand the
    # command line cannot resolve counts as outside. Checked against whatever
    # shell command the tool input actually carries, so it applies uniformly
    # to ws_run_command, terminal_run, and any future command-shaped tool
    # without needing its own category or tool-name special case. A
    # preview_start that names a launch config carries no command of its own,
    # so the one .whisper/launch.json resolves it to, and the one pinned into
    # the call, are checked too (never instead of a "command" key the input
    # may also carry).
    from server.preview.start_policy import floor_commands

    commands = [
        c
        for c in (tool_input.get("command"), *floor_commands(tool_name, tool_input))
        if isinstance(c, str) and c.strip()
    ]
    if commands:
        from server.security.delete_scope import deletes_beyond

        cwd, roots = _delete_roots(tool_name, tool_input)
        if any(deletes_beyond(command, cwd, roots) for command in commands):
            return "rm"

    # Sandbox escalation (sandbox_permissions="danger-full-access"): the same
    # floor as rm. The parameter's entire meaning is "run this ONE command
    # outside the workspace-write OS sandbox, with a human reading the
    # justification first", so outside bypassPermissions no trusted-skill
    # flag, blanket session approval or dontAsk default may cover it. Without this, "Yes, all cli" would
    # silently convert every later escalated call into an unconfined
    # auto-run. In bypassPermissions it runs (approval_floor lifts it).
    if str(tool_input.get("sandbox_permissions") or "").strip() == "danger-full-access":
        return "sandbox-escalation"

    # A terminal_run typed into the user's visible terminal. That shell runs
    # with no OS write confinement, so outside bypassPermissions only a person
    # reading the command keeps a write outside the workspace from happening:
    # a "Yes, all cli" or the Auto classifier never runs it with no
    # card. In bypassPermissions it runs (the user's rule 2026-10-28; see
    # approval_floor).
    if _visible_terminal(tool_name, tool_input):
        return "visible-terminal"

    # MCP tool calls: a dedicated tier on top of (not instead of) everything
    # below. A server/tool marked `approval_mode: "approve"` in
    # mcp_servers.json (server.mcp.MCPManager.get_tool_approval_tier_for_
    # tool_name) is a hard floor, like github-destructive, no bypass mode,
    # trusted-skill, or blanket session approval may skip it, because the
    # user explicitly marked that server as one they don't fully trust.
    # Every other tier ("auto", "prompt", "writes") falls straight through to
    # the standard resolution below unchanged, the per-server config only
    # ever ADDS a gate (server.mcp.MCPManager.call_tool decides whether to
    # ask at all); it never removes the ordinary mode and session approval
    # pipeline for MCP tools like any other tool.
    if category == "mcp":
        from server.mcp import mcp_manager

        if mcp_manager.get_tool_approval_tier_for_tool_name(tool_name) == "approve":
            return "mcp-approve"
    return None


def _visible_terminal(tool_name: str, tool_input: dict) -> bool:
    """A terminal_run typed into the user's own visible terminal."""
    return (
        "terminal_run" in (tool_name, tool_input.get("action"))
        and str(tool_input.get("mode") or "").strip() == "visible"
    )


def _delete_roots(tool_name: str, tool_input: dict) -> tuple[str | None, list[str]]:
    """The working directory a command-shaped call deletes relative to, and
    the roots a delete may stay inside without the rm floor: the workspace the
    call is bound to (the root stamped on a workspace-bound card, else the
    connected one) and the app's own folders (server.infrastructure.paths.
    app_storage_roots). A ``cwd`` in the input is the command's working
    directory, resolved against the workspace when relative; without one the
    command runs in the workspace. No workspace means no working directory for
    a relative operand, so such a delete counts as outside.

    A sandbox terminal_run starts where its executor puts it
    (server.executors.terminal_run.resolve_cwd), and the floor reads that same
    folder. A visible one is typed into the user's shell, which runs wherever
    an earlier command or the user left it and ignores ``cwd``: it has no
    known working directory, so every relative delete there counts as outside.
    """
    from server.infrastructure.paths import app_storage_roots
    from server.workspace.state import get_workspace_path

    ws = tool_input.get("workspace_root") or get_workspace_path()
    roots = ([ws] if ws else []) + list(app_storage_roots())
    if _visible_terminal(tool_name, tool_input):
        return None, roots
    if "terminal_run" in (tool_name, tool_input.get("action")):
        from server.executors.terminal_run import resolve_cwd

        return resolve_cwd(tool_input.get("cwd"), ws), roots
    cwd = tool_input.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        cwd = os.path.expanduser(cwd.strip())
        if not os.path.isabs(cwd):
            cwd = os.path.join(ws, cwd) if ws else None
    else:
        cwd = ws
    return cwd, roots


def targets_app_storage(tool_input: dict) -> bool:
    """Whether the call's file target lies in a folder the app owns for the
    user (server.infrastructure.paths.app_storage_roots): ``~/.whisper`` with
    its rules, profile, scratch, memory and data. Writing there is the point
    of those folders, so the user asked for no approval in any mode; a delete
    there is silent only in bypassPermissions. A relative path resolves
    against the connected workspace, so while a workspace is connected only an
    absolute (or escaping) path can land here."""
    raw = tool_input.get("destination_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return False
    raw = os.path.expanduser(raw.strip())
    if not os.path.isabs(raw):
        from server.workspace.state import get_workspace_path

        ws = get_workspace_path()
        if not ws:
            return False
        raw = os.path.join(ws, raw)
    real = os.path.realpath(raw)
    from server.infrastructure.paths import app_storage_roots

    for root in app_storage_roots():
        r = os.path.realpath(root)
        if real == r or real.startswith(r + os.sep):
            return True
    return False


# Approval categories whose payload "path" (or "destination_path") names the
# file the action writes, saves or deletes. Only these carry a file target
# that can land on one of the app's own config files.
FILE_WRITE_CATEGORIES = frozenset({"write", "delete", "save", "office-script"})


def targets_app_config(tool_input: dict) -> bool:
    """Whether the call's file target is one of the app's own grant and config
    files (server.infrastructure.paths.app_config_paths), a folder that holds
    one, or the plugin/agent-type directories (app_config_dirs).

    The real paths are compared without case, since the Mac disk ignores case,
    and an existing target is also compared by inode, so a symlink, a hard link
    and another spelling of the name are the same file. A relative path
    resolves against the connected workspace, as the write executors resolve
    it."""
    raws = [
        os.path.expanduser(raw.strip())
        for raw in (tool_input.get("path"), tool_input.get("destination_path"))
        if isinstance(raw, str) and raw.strip()
    ]
    if not raws:
        return False
    from server.infrastructure.paths import app_config_dirs, app_config_paths

    configs = [os.path.realpath(p) for p in app_config_paths()]
    dirs = [os.path.realpath(d).casefold() for d in app_config_dirs()]
    inodes = set()
    for config in configs:
        try:
            st = os.stat(config)
        except OSError:
            continue
        inodes.add((st.st_dev, st.st_ino))
    for raw in raws:
        if not os.path.isabs(raw):
            from server.workspace.state import get_workspace_path

            ws = get_workspace_path()
            if not ws:
                continue
            raw = os.path.join(ws, raw)
        real = os.path.realpath(raw)
        lower = real.casefold()
        folder = real.rstrip(os.sep).casefold() + os.sep
        for config in configs:
            name = config.casefold()
            if name == lower or name.startswith(folder):
                return True
        for directory in dirs:
            if lower == directory or lower.startswith(directory + os.sep):
                return True
        try:
            st = os.stat(real)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in inodes:
            return True
    return False


def _app_config_decision(mode: str) -> str | None:
    """How a write to one of the app's own config files behaves per mode (the
    user's table): bypassPermissions allows with no card, dontAsk denies, auto
    lets the classifier decide (None defers to it), and every other mode
    (default, acceptEdits, plan) asks. These files never get the no-card allow
    that the rest of the app's folder gets, and a blanket "yes for all writes"
    never covers them (this is resolved before the session-approval step)."""
    if mode == MODE_BYPASS:
        return "allow"
    if mode == MODE_DONT_ASK:
        return "deny"
    if mode == MODE_AUTO:
        return None
    return "ask"


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
    else resolved it (the caller then falls back to the classifier).

    Evaluation order: hard floors (github-destructive, MCP server marked
    approve, and outside bypass a delete outside the workspace, sandbox
    escalation and a visible terminal run; see approval_floor;
    github-destructive is denied instead when the mode is dontAsk) → app
    storage (a write under ~/.whisper is allowed in every mode, a delete there
    only in bypassPermissions; see targets_app_storage) → bypassPermissions
    (allows everything left, anywhere on the machine) → trusted skill script →
    session approvals ("yes/no for all this session") → a preview_start naming
    an approved launch.json config (allow) → dontAsk → acceptEdits (write
    category only) → auto (defer) → ask.
    """
    floor = approval_floor(tool_name, tool_input, category, mode)
    if floor is not None:
        # A destructive GitHub call never runs without a person, and Don't ask
        # means nobody is there to ask, so it is denied there (stricter than
        # the card).
        if floor == "github-destructive" and mode == MODE_DONT_ASK:
            return "deny"
        return "ask"

    # The app's own grant and config files (server.infrastructure.paths.
    # app_config_paths / app_config_dirs): a write, save or delete of one
    # decides what the assistant may do next, so it follows the permission mode
    # (the user's table: bypass allow, dontAsk deny, auto classifier, every
    # other mode ask) instead of the no-card allow the rest of the folder gets.
    # Checked before the app storage rule below and before the session-approval
    # step, so a blanket "yes for all writes" never covers these files.
    if category in FILE_WRITE_CATEGORIES and targets_app_config(tool_input):
        return _app_config_decision(mode)

    # The app's own folders (~/.whisper: rules, profile, scratch, memory,
    # data) are where these writes belong, so no mode asks for them. Checked
    # before the mode: "in any permission mode" is the user's rule. A delete
    # there is silent only in bypassPermissions; every other mode applies its
    # normal delete rule.
    if targets_app_storage(tool_input) and (category != "delete" or mode == MODE_BYPASS):
        return "allow"

    # Bypass allows everything the floors above left, inside the workspace
    # and outside it (a save to a path the user named, a folder outside the
    # workspace): the user's decision of 2026-11-02.
    if mode == MODE_BYPASS:
        return "allow"
    if auto_allow_trusted:
        return "allow"
    if session_approvals.get(category) == "allow":
        return "allow"
    if session_approvals.get(category) == "deny":
        return "deny"

    # A preview_start naming a .whisper/launch.json config whose command a
    # person approved once starts whatever the mode says
    # (server/preview/start_policy.py): it stands in for the mode defaults
    # below, so the floors and session approvals above still decide first and
    # a session "No for all" keeps refusing it.
    from server.preview.start_policy import starts_without_approval

    if starts_without_approval(tool_name, tool_input):
        return "allow"

    if mode == MODE_DONT_ASK:
        return "deny"
    if mode == MODE_ACCEPT_EDITS and category == "write":
        return "allow"
    if mode == MODE_AUTO:
        return None

    return "ask"


# --- API Routes ---


@router.get("")
async def get_permissions():
    return load_permissions()


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
    save_mode(mode)
    return {"mode": mode}


@router.post("/auto-mode/resume")
async def resume_auto_mode_route(request: Request):
    """One-click override for the auto-mode circuit breaker (mirrors Codex's
    /approve-after-circuit-break): the user asks auto mode to resume
    classifying for the rest of the current turn instead of asking for
    every remaining tool call. Does not touch the persisted permission
    mode, the breaker is turn-scoped, in-memory state."""
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
