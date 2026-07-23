"""
Git API router — REST endpoints for frontend direct git access.

All endpoints require a connected workspace with .git.
Read-only endpoints have no approval check.
Sync git functions are called directly — FastAPI runs them in threadpool.
"""

import asyncio
import json
import os
import shutil
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.git.core import (
    _run_git,
    get_branch,
    get_default_branch,
)
from server.git.diff import fetch_git_diff
from server.workspace import get_workspace_path

router = APIRouter(prefix="/api/git", tags=["git"])


# ── /api/git/changes cache ───────────────────────────────────────────
# 1-second TTL coalesces the assistant tool-loop + UI auto-poll bursts
# that produced 4-6 git subprocess invocations per second. Short enough
# that external editor changes show within ~1s of the next poll.
#
# Bounded at MAX entries (LRU): a long-running session that hops
# between many worktrees would otherwise grow this dict unbounded.
# OrderedDict + move_to_end gives us cheap LRU without pulling in
# functools.lru_cache (which can't be invalidated by external keys).
from collections import OrderedDict  # noqa: E402

_CHANGES_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_CHANGES_CACHE_MAX = 32
_CHANGES_TTL_SECONDS = 1.0


def invalidate_changes_cache(workspace_path: str | None = None) -> None:
    """Drop cached `/api/git/changes` results. Called after any tool that
    can mutate the workspace (writes, deletes, commits) so the next fetch
    sees the new state immediately."""
    if workspace_path is None:
        _CHANGES_CACHE.clear()
    else:
        _CHANGES_CACHE.pop(os.path.normpath(workspace_path), None)


class RestoreRequest(BaseModel):
    path: str


class WorktreeAddRequest(BaseModel):
    branch: str
    path: str | None = None
    create_branch: bool = False


def _require_git_workspace() -> str:
    """Get workspace path and validate it's a git repo. Raises HTTPException if not."""
    ws = get_workspace_path()
    if not ws:
        raise HTTPException(status_code=400, detail="No workspace connected.")
    if not os.path.exists(os.path.join(ws, ".git")):
        raise HTTPException(status_code=400, detail="Workspace is not a git repository.")
    return ws


@router.get("/status")
def git_status_endpoint():
    """Quick status for UI indicators, includes per-file status."""
    ws = _require_git_workspace()
    result = _run_git(["--no-optional-locks", "status", "--porcelain"], cwd=ws)
    files = []
    changed = 0
    untracked = 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        code = line[:2]
        filename = line[2:].strip()
        if not filename:
            continue
        if code == "??":
            untracked += 1
            files.append({"path": filename, "status": "?"})
        else:
            changed += 1
            # Map porcelain codes to simple status letter
            if "D" in code:
                files.append({"path": filename, "status": "D"})
            elif "A" in code:
                files.append({"path": filename, "status": "A"})
            elif "R" in code:
                files.append({"path": filename, "status": "R"})
            else:
                files.append({"path": filename, "status": "M"})
    # Sync state vs the branch's upstream: "+ahead -behind". No upstream (or
    # any git error) reports zeros rather than failing the whole status.
    ahead = behind = 0
    try:
        sync = _run_git(
            ["--no-optional-locks", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
            cwd=ws,
        )
        parts = sync.stdout.strip().split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    except Exception:
        pass
    return {
        "branch": get_branch(ws),
        "clean": changed == 0 and untracked == 0,
        "changed": changed,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
        "files": files,
    }


@router.get("/events")
async def git_events_endpoint():
    """Server-Sent Events stream — fires when the git file watcher detects
    a change to .git/HEAD, .git/config, or the current branch ref.

    Replaces the frontend's 15s `setInterval` poll. Each `git-changed`
    event carries the current branch and head SHA so the panel can
    refetch only when state actually changed.
    """
    from server.git.core import get_cached_branch, get_cached_head
    from server.git.watcher import git_watcher
    from server.workspace import get_workspace_path

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_change() -> None:
        # Runs on the watcher's thread; bounce to the event loop.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:
            # Loop already closed
            pass

    unsubscribe = git_watcher.subscribe(on_change)

    async def stream():
        try:
            # Emit one event immediately so the client gets the current
            # state without waiting for the first change.
            ws = get_workspace_path()
            if ws:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "git-changed",
                            "branch": get_cached_branch(ws),
                            "head": get_cached_head(ws),
                        }
                    )
                    + "\n\n"
                )
            else:
                yield ": no workspace\n\n"

            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment line. Many proxies drop idle
                    # SSE connections after ~60s.
                    yield ": keep-alive\n\n"
                    continue

                # Drain any additional pending change notifications so
                # we coalesce bursts into one event.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                ws = get_workspace_path()
                if not ws:
                    yield "data: " + json.dumps({"type": "no-workspace"}) + "\n\n"
                    continue
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "git-changed",
                            "branch": get_cached_branch(ws),
                            "head": get_cached_head(ws),
                        }
                    )
                    + "\n\n"
                )
        finally:
            unsubscribe()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/changes")
def git_changes_endpoint():
    """Combined status + diff in one round trip with a 1-second TTL cache.

    Replaces two parallel calls (`/api/git/status` + `/api/git/diff`) the
    GitChangesPanel used to make. The cache absorbs the assistant's tool
    loop running `git status` / `git diff` repeatedly between rounds —
    cuts subprocess count by ~70% under load."""
    ws = _require_git_workspace()
    key = os.path.normpath(ws)
    now = time.monotonic()
    cached = _CHANGES_CACHE.get(key)
    if cached and now - cached[0] < _CHANGES_TTL_SECONDS:
        # Refresh LRU position — touching a hot entry keeps it from
        # being evicted by a sweep through cold worktrees.
        _CHANGES_CACHE.move_to_end(key)
        return cached[1]

    # status
    result = _run_git(["--no-optional-locks", "status", "--porcelain"], cwd=ws)
    files = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        code = line[:2]
        filename = line[2:].strip()
        if not filename:
            continue
        if code == "??":
            files.append({"path": filename, "status": "?", "staged": False})
        else:
            staged = code[0] not in (" ", "?")
            if "D" in code:
                status_letter = "D"
            elif "A" in code:
                status_letter = "A"
            elif "R" in code:
                status_letter = "R"
            else:
                status_letter = "M"
            files.append({"path": filename, "status": status_letter, "staged": staged})

    # diff stats
    diff = fetch_git_diff(ws)
    per_file: dict = {}
    files_count = 0
    lines_added = 0
    lines_removed = 0
    if diff is not None:
        files_count = diff.files_count
        lines_added = diff.lines_added
        lines_removed = diff.lines_removed
        for fname, fstat in diff.per_file_stats.items():
            per_file[fname] = {
                "added": fstat.added,
                "removed": fstat.removed,
                "is_binary": fstat.is_binary,
                "is_untracked": fstat.is_untracked,
            }

    response = {
        "branch": get_branch(ws),
        "files": files,
        "files_count": files_count,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "per_file_stats": per_file,
    }
    _CHANGES_CACHE[key] = (now, response)
    _CHANGES_CACHE.move_to_end(key)
    # Evict the coldest entries once we breach the bound. Done after
    # insert so the just-inserted key (now hottest) is always kept.
    while len(_CHANGES_CACHE) > _CHANGES_CACHE_MAX:
        _CHANGES_CACHE.popitem(last=False)
    return response


@router.get("/branch")
def git_branch_endpoint():
    """Current branch name and default branch."""
    ws = _require_git_workspace()
    branch = get_branch(ws)
    default = get_default_branch(ws)
    return {
        "branch": branch,
        "default": default,
        "is_default": branch == default,
    }


@router.get("/diff")
def git_diff_endpoint():
    """Diff summary for UI panel."""
    ws = _require_git_workspace()
    diff = fetch_git_diff(ws)
    if diff is None:
        return {"files_count": 0, "lines_added": 0, "lines_removed": 0, "per_file_stats": {}}
    per_file = {}
    for fname, fstat in diff.per_file_stats.items():
        per_file[fname] = {
            "added": fstat.added,
            "removed": fstat.removed,
            "is_binary": fstat.is_binary,
            "is_untracked": fstat.is_untracked,
        }
    return {
        "files_count": diff.files_count,
        "lines_added": diff.lines_added,
        "lines_removed": diff.lines_removed,
        "per_file_stats": per_file,
    }


@router.get("/show")
def git_show_endpoint(path: str):
    """Return the HEAD version of a file for diffing."""
    ws = _require_git_workspace()
    result = _run_git(["show", f"HEAD:{path}"], cwd=ws, timeout=10)
    if result.returncode != 0:
        return {"content": None, "error": result.stderr.strip()}
    return {"content": result.stdout}


@router.post("/restore")
def git_restore_endpoint(req: RestoreRequest):
    """Undo changes to a single file."""
    ws = _require_git_workspace()
    full_path = os.path.normpath(os.path.join(ws, req.path))
    if not full_path.startswith(os.path.normpath(ws)):
        raise HTTPException(status_code=400, detail="Invalid path.")
    # Check if untracked
    check = _run_git(["ls-files", "--error-unmatch", req.path], cwd=ws)
    if check.returncode != 0:
        # Untracked path — could be a file or a directory (e.g. a newly
        # added folder). ``os.remove`` raises IsADirectoryError on dirs
        # and the UI would think restore succeeded while the path
        # remained on disk. Use rmtree for directories.
        if os.path.isdir(full_path) and not os.path.islink(full_path):
            shutil.rmtree(full_path, ignore_errors=True)
        elif os.path.exists(full_path) or os.path.islink(full_path):
            os.remove(full_path)
        return {"success": True}
    # Tracked file — restore from HEAD
    result = _run_git(["checkout", "HEAD", "--", req.path], cwd=ws)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=result.stderr.strip())
    return {"success": True}


@router.get("/worktrees")
def git_worktrees_endpoint():
    """List git worktrees with the active branch in each."""
    ws = _require_git_workspace()
    result = _run_git(["worktree", "list", "--porcelain"], cwd=ws, timeout=10)
    if result.returncode != 0:
        return {"worktrees": [], "current": ws}

    worktrees: list[dict] = []
    current: dict = {}
    for line in result.stdout.split("\n"):
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree ") :].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()[:7]
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            current["branch"] = ref.replace("refs/heads/", "")
        elif line == "detached":
            current["branch"] = None
        elif line == "bare":
            current["bare"] = True
    if current:
        worktrees.append(current)

    ws_norm = os.path.normpath(ws)
    for w in worktrees:
        w["is_current"] = os.path.normpath(w.get("path", "")) == ws_norm
    return {"worktrees": worktrees, "current": ws}


@router.post("/worktrees")
def git_worktrees_add_endpoint(req: WorktreeAddRequest):
    """Create a new worktree. Path defaults to ../<repo>-<branch> next to the main checkout."""
    ws = _require_git_workspace()
    branch = req.branch.strip()
    if not branch:
        raise HTTPException(status_code=400, detail="branch is required")

    if req.path:
        target = os.path.normpath(os.path.expanduser(req.path))
    else:
        parent = os.path.dirname(os.path.normpath(ws))
        repo_name = os.path.basename(os.path.normpath(ws))
        safe_branch = branch.replace("/", "-")
        target = os.path.join(parent, f"{repo_name}-{safe_branch}")

    if os.path.exists(target):
        raise HTTPException(status_code=400, detail=f"Path already exists: {target}")

    cmd = ["worktree", "add"]
    if req.create_branch:
        cmd += ["-b", branch, target]
    else:
        cmd += [target, branch]

    result = _run_git(cmd, cwd=ws, timeout=30)
    if result.returncode != 0:
        raise HTTPException(
            status_code=400, detail=result.stderr.strip() or "git worktree add failed"
        )
    return {"success": True, "path": target, "branch": branch}


class WorktreeRemoveRequest(BaseModel):
    path: str
    force: bool = False


@router.get("/worktree-session")
def git_worktree_session_endpoint(session_id: str):
    """Return the active WorktreeSession for the given chat session, or null."""
    from server.git.worktree_session import get_session

    s = get_session(session_id)
    if not s:
        return {"session": None}
    return {"session": s.to_dict()}


@router.post("/worktrees/remove")
def git_worktrees_remove_endpoint(req: WorktreeRemoveRequest):
    """Remove a worktree. Refuses to remove the current workspace."""
    ws = _require_git_workspace()
    target = os.path.normpath(os.path.expanduser(req.path))
    if target == os.path.normpath(ws):
        raise HTTPException(status_code=400, detail="Cannot remove the currently active workspace")
    cmd = ["worktree", "remove"]
    if req.force:
        cmd.append("--force")
    cmd.append(target)
    result = _run_git(cmd, cwd=ws, timeout=15)
    if result.returncode != 0:
        raise HTTPException(
            status_code=400, detail=result.stderr.strip() or "git worktree remove failed"
        )
    return {"success": True, "path": target}
