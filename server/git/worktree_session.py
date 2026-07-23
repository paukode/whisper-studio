"""
Worktree as a session concept.

Mirrors Claude Code's `WorktreeSession` (utils/worktree.ts) so the
assistant can enter and exit isolated git worktrees inside a chat
session. While a session is "in" a worktree, every subsequent tool
call operates with cwd set to the worktree path.

Convention-based location: `<repo>/.whisper/worktrees/<flattened-slug>/`.
Branch naming: `worktree-<flattened-slug>`. Both flatten `/` → `+` to
avoid git D/F ref conflicts and nested directory hazards.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("whisper-studio")

# Allowlist matches Claude Code's validateWorktreeSlug: per-segment letters,
# digits, dots, underscores, dashes. Rejects path traversal, leading dash,
# shell metachars, whitespace, NUL.
_SLUG_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_MAX_SLUG_LENGTH = 64


def validate_worktree_slug(slug: str) -> None:
    """Raise ValueError if the slug is unsafe for a path/branch component.

    A valid slug is a `/`-separated path where each segment matches
    [a-zA-Z0-9._-]+, and the whole thing is at most 64 chars. Rejects:
    empty, leading dash, leading slash, '..', double slashes, single-dot
    segments, anything not in the allowlist.
    """
    if not slug:
        raise ValueError("Worktree name cannot be empty")
    if len(slug) > _MAX_SLUG_LENGTH:
        raise ValueError(f"Worktree name too long (max {_MAX_SLUG_LENGTH} chars)")
    if slug.startswith("-"):
        raise ValueError("Worktree name cannot start with '-'")
    if slug.startswith("/") or slug.endswith("/"):
        raise ValueError("Worktree name cannot start or end with '/'")
    segments = slug.split("/")
    for seg in segments:
        if not seg:
            raise ValueError("Worktree name contains empty path segment")
        if seg == "." or seg == "..":
            raise ValueError("Worktree name contains '.' or '..' segment")
        if not _SLUG_SEGMENT_RE.match(seg):
            raise ValueError(
                "Worktree name segments may only contain letters, digits, dots, underscores, dashes"
            )


def flatten_slug(slug: str) -> str:
    """Flatten `/` → `+` so nested slugs become flat filesystem and branch names."""
    return slug.replace("/", "+")


def worktrees_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".whisper", "worktrees")


def worktree_path_for(repo_root: str, slug: str) -> str:
    return os.path.join(worktrees_dir(repo_root), flatten_slug(slug))


def worktree_branch_name(slug: str) -> str:
    return f"worktree-{flatten_slug(slug)}"


# ── Session state ─────────────────────────────────────────────────


@dataclass
class WorktreeSession:
    """In-memory state for a chat session that has entered a worktree."""

    session_id: str
    original_cwd: str
    worktree_path: str
    worktree_name: str
    worktree_branch: str
    original_branch: str | None = None
    original_head_commit: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeSession:
        return cls(**data)


# Module-level state. Keyed by Bedrock chat session_id. Persisted to
# `.whisper_cache/worktree-sessions.json` so server restart preserves
# the in-worktree mapping (matches Claude Code's `--resume` behavior).
_SESSIONS: dict[str, WorktreeSession] = {}

# The harvest feature mutates sessions from _agent_executor worker THREADS
# (set_session on cleanup) while the event loop enters worktrees for other
# agents — so every read/write of _SESSIONS and every rewrite of the persist
# file must hold this lock.
_SESSIONS_LOCK = threading.RLock()

_PERSIST_DIR = os.path.expanduser("~/.whisper_cache")
_PERSIST_FILE = os.path.join(_PERSIST_DIR, "worktree-sessions.json")


def _load_persisted() -> None:
    if not os.path.isfile(_PERSIST_FILE):
        return
    try:
        with open(_PERSIST_FILE) as f:
            data = json.load(f)
        for sid, payload in (data or {}).items():
            try:
                _SESSIONS[sid] = WorktreeSession.from_dict(payload)
            except Exception:
                log.warning("worktree_session: skipping malformed persisted entry for %s", sid)
    except Exception:
        log.exception("worktree_session: failed to load persisted sessions")


def _save_persisted() -> None:
    # Callers already hold _SESSIONS_LOCK; snapshot under it and write.
    try:
        os.makedirs(_PERSIST_DIR, exist_ok=True)
        with open(_PERSIST_FILE, "w") as f:
            json.dump({sid: s.to_dict() for sid, s in _SESSIONS.items()}, f, indent=2)
    except Exception:
        log.exception("worktree_session: failed to persist sessions")


# Load on import so endpoints see prior state immediately.
_load_persisted()


def get_session(session_id: str) -> WorktreeSession | None:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(session_id)


def set_session(session_id: str, session: WorktreeSession | None) -> None:
    with _SESSIONS_LOCK:
        if session is None:
            _SESSIONS.pop(session_id, None)
        else:
            _SESSIONS[session_id] = session
        _save_persisted()


def all_sessions() -> dict[str, WorktreeSession]:
    with _SESSIONS_LOCK:
        return dict(_SESSIONS)


# ── Subprocess: enter / exit ──────────────────────────────────────


def _git_env() -> dict[str, str]:
    """Same hygiene as core._run_git — never hang waiting for credentials."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    return env


def enter_worktree(repo_root: str, slug: str, session_id: str) -> WorktreeSession:
    """Create (or resume) a worktree at the conventional path and record
    the session. Returns the WorktreeSession.

    Resume behavior matches Claude Code: if `<.whisper/worktrees/<slug>>`
    already exists, reuse it rather than running `git worktree add` again.
    """
    validate_worktree_slug(slug)
    from server.git.core import get_branch, get_head

    target = worktree_path_for(repo_root, slug)
    branch = worktree_branch_name(slug)

    original_branch = get_branch(repo_root)
    original_head = get_head(repo_root)

    if not os.path.exists(target):
        os.makedirs(worktrees_dir(repo_root), exist_ok=True)
        # `git worktree add -b <branch> <path> HEAD` creates the worktree
        # branched from current HEAD. -b creates the branch if missing.
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, target, "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
        )
        if result.returncode != 0:
            # If the branch already exists (e.g. user manually created it
            # before), retry without -b. This matches Claude Code's "resume
            # existing worktree" semantics — same slug = same worktree.
            if "already exists" in (result.stderr or ""):
                result = subprocess.run(
                    ["git", "worktree", "add", target, branch],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    stdin=subprocess.DEVNULL,
                    env=_git_env(),
                )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed: {(result.stderr or '').strip() or 'unknown error'}"
                )

    session = WorktreeSession(
        session_id=session_id,
        original_cwd=repo_root,
        worktree_path=target,
        worktree_name=slug,
        worktree_branch=branch,
        original_branch=original_branch,
        original_head_commit=original_head or None,
    )
    set_session(session_id, session)
    return session


def exit_worktree(session_id: str, *, force: bool = False) -> WorktreeSession:
    """Remove the session's worktree and clear the session record.

    Refuses to remove if the worktree has uncommitted changes unless
    `force=True`. The caller (approval executor) is expected to surface
    that as a follow-up confirmation rather than silently nuking work.
    """
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError("No active worktree session for this chat")

    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(session.worktree_path)

    result = subprocess.run(
        cmd,
        cwd=session.original_cwd,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree remove failed: {(result.stderr or '').strip() or 'unknown error'}"
        )

    set_session(session_id, None)
    return session


__all__ = [
    "WorktreeSession",
    "validate_worktree_slug",
    "flatten_slug",
    "worktrees_dir",
    "worktree_path_for",
    "worktree_branch_name",
    "get_session",
    "set_session",
    "all_sessions",
    "enter_worktree",
    "exit_worktree",
]
