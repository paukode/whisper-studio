"""Persistent workspace state: config file, recent workspaces, connect/disconnect,
writability hints, plan-mode flag, and the prompt-payload helper used when a
tool fires without a connected workspace.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
from contextvars import ContextVar as _ContextVar
from dataclasses import dataclass

from .paths import (
    RECENT_WORKSPACES_PATH,
    SAVE_LOCATIONS_PATH,
    WORKSPACE_BACKUPS,
    WORKSPACE_CONFIG_PATH,
    _ws_validate_path,
)

log = logging.getLogger("whisper-studio")

# Serializes every read-modify-write of workspace_config.json: the REST
# connect/disconnect handlers on the event loop, and connect_workspace /
# open_folder_now called from tool threads. Held only around the small file
# read and the atomic replace, never across anything a turn does, so taking
# it on the loop costs microseconds.
CONFIG_LOCK = threading.Lock()


def _write_json_atomically(path: str, data) -> None:
    """Replace ``path`` with ``data`` as JSON in one rename.

    A reader sees the old file or the new one, never a partial one, and a
    failed write (a full disk) leaves the old file whole. No fsync: these
    small state files are rewritten on the event loop, where an fsync under
    heavy disk load could stall every session, and losing the very last
    write to a power cut is harmless.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def load_workspace_config() -> dict:
    try:
        with open(WORKSPACE_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"path": None}


def save_workspace_config(config: dict):
    # Atomic: get_workspace_path reads this file on every tool call, and a
    # truncate-then-write let a reader on another thread catch it empty and
    # conclude no workspace was connected.
    _write_json_atomically(WORKSPACE_CONFIG_PATH, config)


# Bumped each time the USER lets go of the connected workspace: a disconnect,
# or connecting a different folder from the UI. A tool that connects a folder
# itself (git_clone, ws_open_folder) does not bump it, so the turn that ran it
# keeps working in the folder it opened.
_release_epoch = 0


@dataclass(frozen=True)
class WorkspaceLatch:
    """The connected workspace a turn was assembled against.

    ``path`` is the root get_workspace_path returned when the turn started
    (None for a turn that began without one) and ``epoch`` the release epoch
    at that moment. See workspace_lost_message.
    """

    path: str | None
    epoch: int


# The latch of the turn whose tools are running. Set by execute_tool_batch for
# the duration of a batch; tool_router._submit copies it onto worker threads
# with the rest of the context.
_TURN_LATCH: _ContextVar[WorkspaceLatch | None] = _ContextVar("workspace_turn_latch", default=None)


def latch_workspace(read_path=None) -> WorkspaceLatch:
    """Snapshot the connected workspace for a turn that is starting.

    ``read_path`` is the caller's own get_workspace_path (the chat route
    passes the name it imported, so a patched lookup is honoured); it
    defaults to this module's.

    The epoch is read BEFORE the path, and a release bumps it only AFTER its
    config write, so a disconnect racing this call either lands before the
    path read (the turn starts with no workspace) or counts as a later
    release. It can never pair the old path with the new epoch.

    A turn started from inside another turn's tool batch (a subagent, a
    detached one included, or a nested headless run) inherits that turn's
    latch instead: it is doing the parent's work, meant for the parent's
    workspace. A fresh snapshot there would read no workspace once the parent
    is released (a latch that is never released) and then act on whatever
    the user connected in its place. A pinned run keeps its own root.
    """
    ambient = _TURN_LATCH.get()
    if ambient is not None and not _WS_OVERRIDE.get():
        return ambient
    epoch = _release_epoch
    path = (read_path or get_workspace_path)()
    return WorkspaceLatch(path=path, epoch=epoch)


def set_turn_latch(latch: WorkspaceLatch | None):
    """Bind ``latch`` to the current context. Returns the token for reset."""
    return _TURN_LATCH.set(latch)


def reset_turn_latch(token) -> None:
    _TURN_LATCH.reset(token)


def _released(latch: WorkspaceLatch | None, current: str | None) -> bool:
    if latch is None or not latch.path or latch.epoch == _release_epoch:
        return False
    if not current:
        return True
    return os.path.realpath(current) != os.path.realpath(latch.path)


def workspace_lost_message() -> str | None:
    """The refusal a workspace tool gives once the running turn's workspace
    was let go of by the user, or None while it is still there.

    Released means the turn began in a workspace that the user has since
    disconnected, or switched away from in the UI. Reconnecting the same
    folder is not a release (the turn's paths are valid there again), and a
    turn that began with no workspace is never released. Neither is a pinned
    run (a per-task override): it keeps its own root, see get_workspace_path.
    """
    if _WS_OVERRIDE.get():
        return None
    latch = _TURN_LATCH.get()
    current = load_workspace_config().get("path")
    if not _released(latch, current):
        return None
    old = latch.path
    if current:
        what = (
            f"the user disconnected {old} while this turn was running and connected "
            f"{current} instead. Workspace tools are refused for the rest of this turn "
            f"so nothing meant for {old} lands in {current}."
        )
    else:
        what = (
            f"the user disconnected {old} while this turn was running, so workspace "
            "tools are refused for the rest of this turn."
        )
    return (
        f"No workspace connected: {what} Nothing was changed. Do not retry "
        "workspace tools in this turn; tell the user what is left to do."
    )


def _set_connected_path(path: str | None) -> str | None:
    """Persist ``path`` as the connected workspace; returns the previous one.
    Raises when the config cannot be written (the old file stays whole)."""
    with CONFIG_LOCK:
        config = load_workspace_config()
        previous = config.get("path")
        config["path"] = path
        config.pop("mode", None)  # retired field; scrub it from old configs
        save_workspace_config(config)
    return previous


def _release_workspace() -> None:
    """Count one user release. Called only after the config write that
    disconnected or replaced the workspace succeeded (see latch_workspace)."""
    global _release_epoch
    _release_epoch += 1


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
    """The workspace the current call resolves against.

    A per-task override (a worktree-isolated agent, a pinned workflow run)
    wins: that work is frozen to its own root at launch and belongs to the
    session, so a later disconnect does not redirect or stop it. Otherwise
    the connected workspace, except that a turn whose workspace the user let
    go of mid-turn sees none (workspace_lost_message says why), so its
    remaining tool calls cannot act on a folder that was disconnected or on
    the one connected in its place.
    """
    override = _WS_OVERRIDE.get()
    if override:
        return override
    path = load_workspace_config().get("path")
    if _released(_TURN_LATCH.get(), path):
        return None
    return path


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
    _write_json_atomically(RECENT_WORKSPACES_PATH, recent)


def _workspace_prompt_payload(tool_name: str, tool_input: dict, reason: str) -> str:
    """Emit a WS_WORKSPACE_PROMPT marker so the frontend shows a folder picker.

    The picker lets the user choose where the pending write should happen.
    After they pick (or create) a folder and it's connected as the workspace,
    the frontend replays the original intent via a continuation turn so the
    LLM re-issues the tool call against the now-connected workspace.

    The payload carries the two facts the card needs to explain ITSELF:
    ``workspace`` (the root the containment check actually used) and
    ``attempted`` (the absolute path it rejected). Without them the card can
    only show the bare ``reason`` slug, which is unreadable and, worse,
    unfalsifiable: a user looking at a workspace panel that still renders the
    folder they connected has no way to see that the server's root moved
    underneath them (another process writing the shared workspace_config.json
    is the usual culprit).

    ``recent`` is pruned to directories that still exist and never includes the
    currently connected root. Dead entries linger in the recents file for a
    long time — a pytest temp workspace that leaked into it outlives the test
    run by weeks — and the first entry is what the card promotes as its primary
    button, so an unpruned list hands the user a button that can only fail.

    A turn whose workspace the user disconnected mid-turn gets a plain refusal
    instead: the picker would pause the running turn and ask for a folder the
    user just chose to let go of.
    """
    lost = workspace_lost_message()
    if lost:
        return lost
    ws = get_workspace_path()
    ws_real = os.path.realpath(ws) if ws else None

    def _offerable(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        return os.path.realpath(path) != ws_real

    recent = [p for p in load_recent_workspaces() if _offerable(p)][:5]
    suggested = recent[0] if recent else os.path.expanduser("~/Documents")

    # The path the check rejected. Only meaningful when a workspace WAS
    # connected: with no workspace there is nothing to have been outside of,
    # and the tool's relative path has no absolute form yet.
    attempted = None
    raw_path = tool_input.get("path")
    if ws and raw_path:
        attempted = os.path.realpath(os.path.join(ws, str(raw_path)))

    payload = json.dumps(
        {
            "reason": reason,
            "tool_name": tool_name,
            "tool_input": {k: v for k, v in tool_input.items() if not k.startswith("__")},
            "suggested": suggested,
            "recent": recent,
            "workspace": ws,
            "attempted": attempted,
        }
    )
    return f"[WS_WORKSPACE_PROMPT]{payload}"


# Appended to every successful file-creating approval outcome. The moment the
# model reads a write result is the moment it picks its next attempt's path;
# without this line, an iterating model invents a fresh name per attempt and
# strews 'v2'/'final'/'clean' copies across the user's folder.
REVISION_HINT = (
    "If a follow-up produces a revised version of this file, write it to this "
    "exact same destination — the write replaces the file — do NOT save it under "
    "a new name unless the user asks for a separate copy."
)


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
    workspace), a plain "Error: ..." string, or the refusal for a turn whose
    workspace was disconnected mid-turn. That turn's workspace-relative path
    was meant for the folder it lost, so it is not quietly redirected into
    ~/Documents; an explicit destination_path still works.
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

    lost = workspace_lost_message()
    if lost:
        return lost
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
    _write_json_atomically(SAVE_LOCATIONS_PATH, saved)


def clear_workspace_scoped_state() -> None:
    """Drop state scoped to a workspace that is going away.

    Without this, per-session shell cwds keep running read-only commands in
    the old folder and the worktree registry resolves stale branch names.
    Called both on an actual workspace switch (connect_workspace) and on
    disconnect — disconnecting represents "no workspace connected" regardless
    of what gets connected next, so it must not defer this to whatever
    connect happens to follow. A disconnect immediately followed by
    reconnecting to a DIFFERENT path used to skip this entirely: connect's own
    switch check only fires on a truthy `previous`, and disconnect had
    already nulled it out.

    The cwd reset goes through cwd_tracker.clear_all, which also retires every
    command still running: one that finishes after this point cannot write
    the old workspace's cwd back and leak it into the next workspace.
    """
    from server import cwd_tracker
    from server.workspace.executors import _WORKTREES

    cwd_tracker.clear_all()
    _WORKTREES.clear()


def _point_git_watcher_at(path: str | None) -> None:
    # Re-target the git file watcher so its cache invalidates on the new
    # repo's branch/HEAD changes and the SSE subscribers (panel, terminal
    # header) update without polling.
    from server.git.watcher import git_watcher

    git_watcher.set_workspace(path)


def disconnect_workspace() -> list[str]:
    """Disconnect the workspace. Returns one warning per teardown step that
    failed (each is logged too).

    Raises only when the config write itself fails, and then nothing changed:
    the write is atomic, so the workspace is still connected and the caller
    reports the error instead of pretending it disconnected.

    Everything here is in memory apart from that one small file write, so it
    never waits on anything a running turn holds. It stops no turn and kills
    no process a session started: terminals, background shells, previews,
    LSP servers and pinned agent runs belong to the session and keep running.
    A turn that is still running finds out at its next workspace tool call,
    which is refused (workspace_lost_message).
    """
    previous = _set_connected_path(None)
    if previous:
        _release_workspace()
    warnings: list[str] = []
    for label, step in (
        ("undo backups", WORKSPACE_BACKUPS.clear),
        ("session shell state", clear_workspace_scoped_state),
        ("git watcher", lambda: _point_git_watcher_at(None)),
    ):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - reported, and the rest still run
            log.exception("workspace disconnect: resetting the %s failed", label)
            warnings.append(f"Could not reset the {label}: {e}")
    return warnings


def connect_workspace(path: str, *, by_user: bool = False) -> str:
    """Mark `path` as the active workspace, persist it, refresh recents, and
    re-point the git file watcher. Returns the canonicalised realpath that
    was stored. Raises ValueError if the directory does not exist.

    Shared by the REST endpoint (POST /api/workspace/connect) and tools that
    create a workspace as a side effect (e.g. git_clone). Keep both call
    sites in sync by going through this helper.

    ``by_user`` marks the UI's own connect: switching away from a workspace
    there releases it for any turn still running in it, exactly like a
    disconnect. A tool connecting a folder mid-turn does not, so that turn
    goes on working in the folder it opened.

    A tool in a turn whose workspace the user let go of may not connect one
    (ValueError with the reason): it would bring back the folder the user
    just disconnected, or replace the one they connected instead.
    """
    if not by_user:
        lost = workspace_lost_message()
        if lost:
            raise ValueError(lost)
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise ValueError(f"Directory not found: {real}")
    previous = _set_connected_path(real)
    switched = bool(previous) and os.path.realpath(previous) != real
    if switched and by_user:
        _release_workspace()
    save_recent_workspace(real)
    WORKSPACE_BACKUPS.clear()
    # On an ACTUAL workspace change, drop state scoped to the old workspace so
    # it can't bleed into the new one. Without this, per-session shell cwds keep
    # running read-only commands in the old folder, and the worktree registry
    # resolves stale branch names. Skip on the initial connect / reconnect to
    # the same path so we don't needlessly wipe live session state.
    if switched:
        clear_workspace_scoped_state()
    _point_git_watcher_at(real)
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
