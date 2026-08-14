"""
Working directory persistence across shell commands per session.

Appends `; pwd` to commands to capture the final working directory,
then stores it per session so the next command starts from there.
"""

import contextvars
import os
import threading

_lock = threading.Lock()
# tracking key -> absolute cwd path. The key is session_id alone outside
# worktree isolation; see _key() for what changes under it.
_session_cwd: dict[str, str] = {}
# Overrides an agent is still running under. A write for a retired override
# is refused rather than silently resurrecting the entry clear_override just
# removed — see the race this closes in update_cwd's docstring.
_live_overrides: set[str] = set()

# Per-agent cwd identity. Worktree isolation already separates agents by
# their unique worktree path, but agents that are NOT isolated still share
# one session_id — and, since workflow runs are pinned, one workspace path
# too. Without a third component, sibling agents in the same run share a cwd
# slot: one agent's `cd sub` silently relocates the next agent's commands.
# Set per run_agent call and inherited by anything it dispatches (contextvars
# copy into asyncio tasks and, via tool_router._submit, onto worker threads).
_CWD_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("cwd_scope", default=None)


def set_cwd_scope(scope: str | None):
    """Give the current task its own cwd slot. Returns a token for reset."""
    return _CWD_SCOPE.set(scope)


def reset_cwd_scope(token) -> None:
    _CWD_SCOPE.reset(token)


def _active_override() -> str | None:
    """The current task's worktree override, or None outside isolation.
    Lazy-imported: cwd_tracker is a small leaf module and workspace.state
    pulls in a much larger graph."""
    try:
        from server.workspace.state import get_workspace_override

        return get_workspace_override()
    except Exception:  # noqa: BLE001 — never let tracking break a command
        return None


def _key(session_id: str, override: str | None) -> str:
    """Fold the active override AND the per-agent scope into the tracking key.

    Without the override, a worktree-isolated agent shares its cwd slot with
    the parent session: a cwd the user or another agent left behind in the
    ORIGINAL checkout would silently become the isolated agent's
    effective_cwd, and an isolated agent that itself cd's around would leak
    that path back out.

    Without the scope, agents that are NOT worktree-isolated still collide:
    every agent in a workflow run shares one session_id and one pinned
    workspace, so parallel siblings would read and overwrite each other's
    working directory. Both absent (ordinary interactive chat) leaves the key
    exactly as it always was: the bare session_id.
    """
    key = f"{session_id}\x00{override}" if override else session_id
    scope = _CWD_SCOPE.get()
    return f"{key}\x00{scope}" if scope else key


def _is_within(path: str, root: str) -> bool:
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except OSError:
        return False
    return p == r or p.startswith(r + os.sep)


def get_cwd(session_id: str, default: str) -> str:
    """Get the persisted cwd for a session, or default if none.

    Under worktree isolation, a stored cwd is trusted only if it is actually
    inside that override — belt-and-suspenders alongside the per-worktree key,
    so a cwd from any other source can never put a command back outside the
    agent's own worktree.
    """
    override = _active_override()
    key = _key(session_id, override)
    with _lock:
        cwd = _session_cwd.get(key)
    if cwd and os.path.isdir(cwd) and (not override or _is_within(cwd, override)):
        return cwd
    return default


def register_override(override: str) -> None:
    """Mark a worktree override as live, so a write against it is trusted until
    clear_override retires it.

    Call this once, right after set_workspace_override, for every override
    that will call update_cwd. Without it, a command still running when its
    agent is cancelled can finish AFTER clear_override has already popped the
    entry — the command's own update_cwd call runs in a frozen copy of the
    override contextvar (dispatched to a worker thread before cancellation)
    and would otherwise resurrect the retired entry with no owner left to
    clear it again.
    """
    if not override:
        return
    with _lock:
        _live_overrides.add(override)


def update_cwd(session_id: str, cwd: str):
    """Store the working directory for a session."""
    override = _active_override()
    if override and not _is_within(cwd, override):
        return  # never let an isolated agent's cwd escape its own worktree
    key = _key(session_id, override)
    with _lock:
        # A write for an override that clear_override already retired is a
        # command that outlived its agent (see register_override) — refuse it
        # rather than resurrecting an entry nothing will ever clear again.
        if override and override not in _live_overrides:
            return
        _session_cwd[key] = cwd


def clear_session(session_id: str):
    """Remove persisted cwd for a session, including any isolated-agent
    entries nested under it."""
    prefix = session_id + "\x00"
    with _lock:
        _session_cwd.pop(session_id, None)
        for k in [k for k in _session_cwd if k.startswith(prefix)]:
            _session_cwd.pop(k, None)


def clear_override(session_id: str, override: str) -> None:
    """Drop the cwd entry scoped to one worktree, once that worktree is torn
    down. Without this every isolated agent run leaves a permanently orphaned
    entry for the life of the process — each worktree path is unique, so
    nothing else will ever look it up again.

    Retirement and the dict pop happen under the SAME lock acquisition as
    update_cwd's liveness check, so whichever runs second wins deterministically
    — a still-in-flight write either lands just before retirement (harmless,
    cleaned up here) or is cleanly refused just after (see update_cwd).
    """
    if not override:
        return
    with _lock:
        _live_overrides.discard(override)
        _session_cwd.pop(_key(session_id, override), None)


def clear_scope(session_id: str, scope: str) -> None:
    """Drop every cwd entry belonging to one agent's scope, whichever
    override it ran under. Called when the agent finishes so a long-lived
    server does not accumulate one entry per agent forever."""
    if not scope:
        return
    suffix = "\x00" + scope
    with _lock:
        for k in [k for k in _session_cwd if k.startswith(session_id) and k.endswith(suffix)]:
            _session_cwd.pop(k, None)


def wrap_command_for_cwd(command: str) -> str:
    """Append pwd capture to a command.

    The last line of stdout will be the final working directory.
    """
    return f"{command}\necho __CWD_MARKER__; pwd"


def extract_cwd_from_output(output: str) -> tuple[str, str]:
    """Extract and strip the cwd from command output.

    Returns (clean_output, cwd_path). If marker not found,
    returns (output, "").
    """
    marker = "__CWD_MARKER__"
    idx = output.rfind(marker)
    if idx == -1:
        return output, ""
    clean = output[:idx].rstrip("\n")
    after = output[idx + len(marker) :].strip()
    # pwd output is the first line after marker
    lines = after.splitlines()
    cwd = lines[0].strip() if lines else ""
    return clean, cwd
