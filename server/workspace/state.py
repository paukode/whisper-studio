"""Persistent workspace state: config file, recent workspaces, connect/disconnect,
writability hints, plan-mode flag, and the prompt-payload helper used when a
tool fires without a connected workspace.
"""

import json
import os
from contextvars import ContextVar as _ContextVar

from .paths import (
    RECENT_WORKSPACES_PATH,
    SAVE_LOCATIONS_PATH,
    WORKSPACE_BACKUPS,
    WORKSPACE_CONFIG_PATH,
    _ws_validate_path,
)


def load_workspace_config() -> dict:
    try:
        with open(WORKSPACE_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"path": None}


def save_workspace_config(config: dict):
    os.makedirs(os.path.dirname(WORKSPACE_CONFIG_PATH), exist_ok=True)
    with open(WORKSPACE_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# Per-task effective workspace override (worktree-isolated agents). Set via
# set_workspace_override(); tools dispatched from that coroutine context
# resolve every path against the agent's own worktree instead of the global
# root. Thread propagation happens at the tool_router submission helper.
_WS_OVERRIDE: _ContextVar[str | None] = _ContextVar("workspace_override", default=None)


def set_workspace_override(path: str | None):
    """Set (or clear with None) the effective workspace for this context.
    Returns the Token for reset."""
    return _WS_OVERRIDE.set(path)


def reset_workspace_override(token) -> None:
    _WS_OVERRIDE.reset(token)


def get_workspace_override() -> str | None:
    """The raw per-task override, or None outside worktree isolation.

    Distinct from get_workspace_path(), which falls back to the globally
    connected workspace when no override is set — callers that need to know
    specifically whether isolation is active (e.g. cwd_tracker) want this one.
    """
    return _WS_OVERRIDE.get()


def get_workspace_path() -> str | None:
    override = _WS_OVERRIDE.get()
    if override:
        return override
    return load_workspace_config().get("path")


def load_recent_workspaces() -> list[str]:
    try:
        with open(RECENT_WORKSPACES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def save_recent_workspace(path: str):
    recent = load_recent_workspaces()
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    recent = recent[:10]
    os.makedirs(os.path.dirname(RECENT_WORKSPACES_PATH), exist_ok=True)
    with open(RECENT_WORKSPACES_PATH, "w") as f:
        json.dump(recent, f, indent=2)


def _workspace_prompt_payload(tool_name: str, tool_input: dict, reason: str) -> str:
    """Emit a WS_WORKSPACE_PROMPT marker so the frontend shows a folder picker.

    The picker lets the user choose where the pending write should happen.
    After they pick (or create) a folder and it's connected as the workspace,
    the frontend replays the original intent via a continuation turn so the
    LLM re-issues the tool call against the now-connected workspace."""
    recent = load_recent_workspaces()[:5]
    suggested = recent[0] if recent else os.path.expanduser("~/Documents")
    payload = json.dumps(
        {
            "reason": reason,
            "tool_name": tool_name,
            "tool_input": {k: v for k, v in tool_input.items() if not k.startswith("__")},
            "suggested": suggested,
            "recent": recent,
        }
    )
    return f"[WS_WORKSPACE_PROMPT]{payload}"


def resolve_write_destination(tool_name: str, tool_input: dict) -> tuple[str, str] | str:
    """Resolve where a 'create a new file' tool should write its content.

    One shared resolver for every tool that creates a brand-new file
    (ws_create_file, save_file, and the create_docx/pptx/xlsx/pdf document
    tools) so "no workspace connected" behaves identically no matter which
    tool the model happens to call.

    Per explicit user feedback, saving a file NEVER asks "where?" and never
    forces connecting a workspace. The flow is:
    - destination_path present (the model resolved it from the user's own
      words, e.g. "in Downloads" -> ~/Downloads/report.docx): use it.
    - No destination but a workspace is connected: `path` is resolved
      workspace-relative, validated against escaping the workspace root.
    - Neither: default to ~/Documents/<filename> — the approval card shows
      the full path and is the single human gate; the model's reply tells
      the user where the file went.

    Returns ("workspace", full_path) or ("absolute", destination_path) on
    success. On failure returns the string to return directly from the
    executor: a [WS_WORKSPACE_PROMPT] sentinel (path escaping a connected
    workspace) or a plain "Error: ..." string.
    """
    destination_path = tool_input.get("destination_path")
    if destination_path:
        raw = os.path.expanduser(str(destination_path))
        if not os.path.isabs(raw):
            return "Error: destination_path must be an absolute path (~ is allowed)."
        return "absolute", os.path.realpath(raw)

    ws = get_workspace_path()
    if ws:
        path = tool_input.get("path", "")
        if not path:
            return "Error: path is required."
        full = os.path.join(ws, path)
        if not _ws_validate_path(full, ws):
            return _workspace_prompt_payload(tool_name, tool_input, "outside_workspace")
        return "workspace", full

    filename = os.path.basename(str(tool_input.get("path") or "")) or "document"
    return "absolute", os.path.join(os.path.expanduser("~/Documents"), filename)


def load_save_locations() -> list[str]:
    """Directories written to via the one-shot save_file flow. Small, capped
    list — never 'connected' or 'indexed' — that lets a file saved this way
    stay clickable/revealable via /open-with and /reveal afterward without
    the heavyweight persistent-workspace machinery."""
    try:
        with open(SAVE_LOCATIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def record_save_location(path: str) -> None:
    """Record `path` (a directory) as a recent save_file destination. Same
    insert-front/dedupe/cap-at-10 pattern as save_recent_workspace, in its
    own small JSON file so it never mixes with the persistent-workspace
    recents list."""
    saved = load_save_locations()
    if path in saved:
        saved.remove(path)
    saved.insert(0, path)
    saved = saved[:10]
    os.makedirs(os.path.dirname(SAVE_LOCATIONS_PATH), exist_ok=True)
    with open(SAVE_LOCATIONS_PATH, "w") as f:
        json.dump(saved, f, indent=2)


def connect_workspace(path: str) -> str:
    """Mark `path` as the active workspace, persist it, refresh recents, and
    re-point the git file watcher. Returns the canonicalised realpath that
    was stored. Raises ValueError if the directory does not exist.

    Shared by the REST endpoint (POST /api/workspace/connect) and tools that
    create a workspace as a side effect (e.g. git_clone). Keep both call
    sites in sync by going through this helper.
    """
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise ValueError(f"Directory not found: {real}")
    config = load_workspace_config()
    previous = config.get("path")
    switched = bool(previous) and os.path.realpath(previous) != real
    config["path"] = real
    config.pop("mode", None)  # retired field; scrub it from old configs
    save_workspace_config(config)
    save_recent_workspace(real)
    WORKSPACE_BACKUPS.clear()
    # On an ACTUAL workspace change, drop state scoped to the old workspace so
    # it can't bleed into the new one. Without this, per-session shell cwds keep
    # running read-only commands in the old folder, and the worktree registry
    # resolves stale branch names. Skip on the initial connect / reconnect to
    # the same path so we don't needlessly wipe live session state.
    if switched:
        # No clear-all helper exists on either module (only per-session
        # cwd_tracker.clear_session), so reset the module-level registries.
        from server.cwd_tracker import _session_cwd
        from server.workspace.executors import _WORKTREES

        _session_cwd.clear()
        _WORKTREES.clear()
    # Re-target the git file watcher so its cache invalidates on the new
    # repo's branch/HEAD changes and the SSE subscribers (panel, terminal
    # header) update without polling.
    from server.git.watcher import git_watcher

    git_watcher.set_workspace(real)
    return real


def _check_writable(path: str) -> bool:
    """Best-effort writability hint for a workspace folder.

    Returns False only when we have a clear signal the folder cannot be
    written to (not a directory, OSError, or os.access denies W_OK).
    Returns True otherwise — including ambiguous cases where os.access
    is unreliable (network mounts, root user, macOS extended ACLs). The
    frontend treats False as a soft warning ("note: this folder appears
    read-only, writes will be confirmed on first attempt"), never as a
    hard refusal, so a fuzzy check never blocks a legit workspace.

    The actual source of truth for writability is the next real write
    attempt, which surfaces a precise OS error if it fails. This pre-check
    is purely an early-feedback affordance.
    """
    try:
        if not os.path.isdir(path):
            return False
        return os.access(path, os.W_OK)
    except OSError:
        return False


def is_plan_mode() -> bool:
    """Feature 4: Returns True if the workspace is in plan mode (read-only).

    Single source of truth: the permissions mode setting.
    """
    from server.security.permissions import MODE_PLAN, load_permissions

    return load_permissions().get("mode") == MODE_PLAN
