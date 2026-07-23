"""Bring a finished agent's worktree work back into the spawning workspace.

Worktree-isolated agents may leave their work COMMITTED (they have git tools),
UNCOMMITTED, or a mix. A branch merge is a no-op when nothing was committed,
and a HEAD-relative diff misses whatever the agent committed — either way the
work stranded in ``.whisper/worktrees/agent-*``.

Harvest materializes the worktree's full delta SINCE THE BRANCH POINT (base
commit) into the ORIGINATING working tree, UNCOMMITTED, so ``git status`` shows
the agent's new/modified/deleted files for normal review. It works file by file
off the git object database (blob SHAs are shared between a repo and its
worktrees), which is robust for committed work, binary files, deletions, and
executable bits — and lets non-conflicting files land even when one file
collides. A file is a CONFLICT only when the main tree changed it away from the
base AND differently from the agent; those files are left in the worktree,
which is kept, and the note says exactly where. Nothing is ever committed or
force-resolved on the user's behalf.

Materialization is serialized process-wide (`_APPLY_LOCK`): parallel agents
finishing together write to the one shared tree one at a time, so a second
agent sees the first's files as the current state.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from server.git.worktree_session import _git_env, set_session

log = logging.getLogger("whisper-studio")

_APPLY_LOCK = threading.Lock()
_GIT_TIMEOUT = 60
# git's well-known empty-tree object id (sha1). Used only as a last-resort base
# when the branch point is unknown AND git can't report the repo's empty tree.
_EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class _PathConflict(Exception):
    """The main tree holds something (a non-empty directory) at the agent's
    target path that we must not clobber — treated as a per-file conflict, not a
    hard error, so the rest of the harvest still applies."""


def _is_null_sha(sha: str) -> bool:
    """True for git's all-zeros null blob id (an addition/deletion sentinel),
    independent of hash length (40 hex for sha1, 64 for sha256)."""
    return not sha or set(sha) == {"0"}


def _run(args: list[str], cwd: str):
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        stdin=subprocess.DEVNULL,
        env=_git_env(),
    )


def _text(r) -> str:
    return (r.stdout or b"").decode(errors="replace")


def _rm_path(abspath: str) -> None:
    """Remove a file or symlink at abspath. A directory is removed ONLY if it is
    empty (os.rmdir); a NON-empty directory raises _PathConflict so we never
    ``rmtree`` a directory that holds the user's untracked files just because an
    agent added a file of the same name. A legitimate directory->file swap works
    because the directory's tracked files are deleted first (deletions run before
    writes), leaving it empty by the time we get here."""
    if not os.path.lexists(abspath):
        return
    if os.path.isdir(abspath) and not os.path.islink(abspath):
        try:
            os.rmdir(abspath)
        except OSError as e:  # ENOTEMPTY -> would destroy untracked content
            raise _PathConflict(abspath) from e
    else:
        os.remove(abspath)


def _remove_worktree(
    repo_root: str, worktree_path: str, branch: str, session_key: str
) -> tuple[bool, bool]:
    """Remove the worktree + its branch and clear the session record. Returns
    (worktree_removed, branch_deleted) so the caller reports cleanup truthfully.

    NOTE: ``git worktree remove --force`` also discards any GITIGNORED files the
    agent created in the worktree (build artifacts, local configs). Harvest only
    carries git-tracked changes, so those isolated artifacts are intentionally
    not brought back."""
    removed = False
    branch_deleted = False
    try:
        r = _run(["git", "worktree", "remove", "--force", worktree_path], cwd=repo_root)
        removed = r.returncode == 0
        if removed:
            branch_deleted = _run(["git", "branch", "-D", branch], cwd=repo_root).returncode == 0
        else:
            log.warning(
                "worktree remove failed for %s: %s",
                worktree_path,
                (r.stderr or b"").decode(errors="replace").strip(),
            )
    except Exception as e:  # noqa: BLE001 — cleanup must never raise
        log.warning("worktree cleanup error for %s: %s", worktree_path, e)
    try:
        set_session(session_key, None)
    except Exception:  # noqa: BLE001
        pass
    return removed, branch_deleted


def _changed_entries(worktree_path: str, base: str) -> list[dict] | None:
    """Raw per-file delta of the worktree (committed + staged + unstaged) vs the
    base commit. Returns None on a git error, [] when there is nothing to bring
    back. Renames are decomposed into delete+add (`--no-renames`)."""
    if _run(["git", "add", "-A"], cwd=worktree_path).returncode != 0:
        return None
    # --no-abbrev: raw output abbreviates blob SHAs by default, but we compare
    # them against full SHAs from git hash-object, so force full-length here.
    raw = _run(
        ["git", "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", base],
        cwd=worktree_path,
    )
    if raw.returncode != 0:
        return None
    # -z format: repeated ":<smode> <dmode> <ssha> <dsha> <status>\0<path>\0"
    data = raw.stdout.decode("utf-8", "surrogateescape")
    toks = data.split("\0")
    out: list[dict] = []
    i = 0
    while i < len(toks):
        meta = toks[i]
        if not meta.startswith(":"):
            i += 1
            continue
        parts = meta[1:].split()
        if len(parts) < 5 or i + 1 >= len(toks):
            break
        _smode, dmode, ssha, dsha, status = parts[0], parts[1], parts[2], parts[3], parts[4]
        path = toks[i + 1]
        out.append(
            {
                "dmode": dmode,
                "base_sha": ssha,
                "target_sha": dsha,
                "status": status[0],
                "path": path,
            }
        )
        i += 2
    return out


def _hash_worktree_file(repo_root: str, path: str) -> str | None:
    """Blob SHA of the main tree's CURRENT file at path, or None if absent.

    A symlink must be hashed as git stores it (blob = the link-target string),
    NOT by following it — `git hash-object <path>` dereferences the link and
    hashes the target's content, which would defeat conflict detection and let
    a user's local symlink be silently clobbered."""
    abspath = os.path.join(repo_root, path)
    if not os.path.lexists(abspath):
        return None
    if os.path.islink(abspath):
        target = os.readlink(abspath).encode("utf-8", "surrogateescape")
        r = subprocess.run(
            ["git", "hash-object", "--stdin"],
            cwd=repo_root,
            input=target,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            env=_git_env(),
        )
        return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else None
    r = _run(["git", "hash-object", "--", abspath], cwd=repo_root)
    return _text(r).strip() if r.returncode == 0 else None


def _materialize(repo_root: str, path: str, blob_sha: str, dmode: str) -> None:
    """Write the agent's version of a file into the working tree at ``path``,
    reproducing the git-recorded type and mode: a regular file (0644/0755, the
    exec bit set OR cleared to match dmode) or a symlink (mode 120000, whose
    blob content is the link target). Any existing file/symlink at the path is
    replaced so a type change (file <-> symlink) is honored."""
    is_symlink = dmode == "120000"
    # For regular files, run the blob through the repo's smudge filters
    # (--filters --path) so gitattributes are honored — EOL normalization and
    # Git-LFS pointers resolve to what a real checkout would write. Symlinks
    # take the raw blob (its content is the literal link target).
    cat_args = (
        ["git", "cat-file", "blob", blob_sha]
        if is_symlink
        else ["git", "cat-file", "--filters", "--path", path, blob_sha]
    )
    content = subprocess.run(
        cat_args,
        cwd=repo_root,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        stdin=subprocess.DEVNULL,
        env=_git_env(),
    ).stdout
    abspath = os.path.join(repo_root, path)
    _rm_path(abspath)  # replace cleanly (file <-> symlink <-> directory type change)
    os.makedirs(os.path.dirname(abspath) or ".", exist_ok=True)
    if is_symlink:
        # Symlink: the blob content is the target path (no trailing newline).
        os.symlink(content.decode("utf-8", "surrogateescape"), abspath)
        return
    with open(abspath, "wb") as f:
        f.write(content)
    # Set the mode EXACTLY as git records it, so a 755->644 de-escalation is
    # applied (not just 644->755), mirroring what a checkout would do.
    os.chmod(abspath, 0o755 if dmode.endswith("755") else 0o644)


def _classify(repo_root: str, e: dict) -> str:
    """apply | noop | conflict for one changed file, comparing the main tree's
    current content to the base and to the agent's target."""
    main_sha = _hash_worktree_file(repo_root, e["path"])
    base_present = not _is_null_sha(e["base_sha"])
    base_sha = e["base_sha"] if base_present else None

    main_matches_base = (main_sha == base_sha) if base_present else (main_sha is None)
    if main_matches_base:
        return "apply"  # main untouched since the branch point — safe to write
    if e["status"] == "D":
        return "noop" if main_sha is None else "conflict"
    if main_sha == e["target_sha"]:
        return "noop"  # already identical to what the agent produced
    return "conflict"


def harvest_agent_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
    session_key: str,
    apply_changes: bool,
    base_commit: str | None = None,
) -> dict:
    """Materialize a finished agent's worktree changes into ``repo_root``.

    Returns ``{"status", "files", "conflicts", "note"}``. status is:
    ``applied`` (all changes landed uncommitted, worktree removed), ``partial``
    (some landed, some conflicted — worktree kept), ``clean`` (nothing to bring
    back, worktree removed), ``kept`` (not applied — agent incomplete, or all
    files conflict; worktree kept), or ``error``. The note is written for the
    user/model and always states where the files ARE.
    """
    try:
        if not os.path.isdir(worktree_path):
            set_session(session_key, None)
            return {"status": "clean", "files": 0, "conflicts": 0, "note": ""}

        # Diff against the BRANCH POINT so committed agent work is included. If
        # the base is unknown (repo had no commit when the worktree was made),
        # fall back to the empty tree so everything reads as new — never the
        # worktree's own HEAD, which would hide whatever the agent committed.
        base = base_commit
        if not base:
            empty = _run(["git", "hash-object", "-t", "tree", "/dev/null"], cwd=worktree_path)
            base = _text(empty).strip() or _EMPTY_TREE_SHA1
        entries = _changed_entries(worktree_path, base)
        if entries is None:
            return {
                "status": "error",
                "files": 0,
                "conflicts": 0,
                "note": f"could not inspect the worktree at {worktree_path}; it was left in place.",
            }
        if not entries:
            removed, _ = _remove_worktree(repo_root, worktree_path, branch, session_key)
            return {
                "status": "clean",
                "files": 0,
                "conflicts": 0,
                "note": "worktree had no changes; removed it and its branch."
                if removed
                else f"worktree had no changes; could not remove {worktree_path} (branch {branch}).",
            }

        n = len(entries)
        if not apply_changes:
            return {
                "status": "kept",
                "files": n,
                "conflicts": 0,
                "note": (
                    f"agent did not finish; its {n} changed file(s) were NOT applied and remain "
                    f"in the worktree at {worktree_path} (branch {branch})."
                ),
            }

        # Serialize the write phase: concurrent harvests share one working tree,
        # and a later agent must see an earlier one's files as current state.
        with _APPLY_LOCK:
            plan = [(e, _classify(repo_root, e)) for e in entries]
            conflicts = [e["path"] for e, c in plan if c == "conflict"]
            to_apply = [e for e, c in plan if c == "apply"]
            # Deletions first, deepest paths first, so replacing a directory
            # with a file of the same name (its children are deleted before the
            # file is written) works and empty parents are cleared.
            dels = sorted(
                (e for e in to_apply if e["status"] == "D"), key=lambda e: e["path"], reverse=True
            )
            writes = [e for e in to_apply if e["status"] != "D"]
            done = 0
            try:
                for e in dels:
                    _rm_path(os.path.join(repo_root, e["path"]))
                    done += 1
                for e in writes:
                    try:
                        _materialize(repo_root, e["path"], e["target_sha"], e["dmode"])
                        done += 1
                    except _PathConflict:
                        # A non-empty directory holds the user's untracked files
                        # at this path — never destroy it; record a conflict and
                        # keep going with the other files.
                        conflicts.append(e["path"])
            except Exception as we:  # noqa: BLE001 — report partial state honestly
                log.warning("worktree harvest write error for %s: %s", worktree_path, we)
                return {
                    "status": "error",
                    "files": done,
                    "conflicts": len(conflicts),
                    "note": (
                        f"applied {done} file(s) before a write error ({we}); the working tree in "
                        f"{repo_root} is PARTIALLY modified — review git status. The worktree "
                        f"remains at {worktree_path} (branch {branch})."
                    ),
                }

        cur = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
        cur_branch = _text(cur).strip() or "the current branch"
        # `done` (files actually written/deleted), not len(to_apply): a write can
        # turn into a conflict when the main tree holds an untracked directory.
        applied_n = done
        commit_hint = (
            "review with git status / git diff; new files are untracked, so "
            "`git add` them before committing (git commit -a alone would skip them)"
        )

        if not conflicts:
            removed, branch_deleted = _remove_worktree(
                repo_root, worktree_path, branch, session_key
            )
            if removed and branch_deleted:
                tail = f"Worktree and branch {branch} removed."
            elif removed:
                tail = (
                    f"Worktree removed; branch {branch} could not be deleted — remove it manually."
                )
            else:
                tail = (
                    f"NOTE: could not remove the worktree at {worktree_path} (branch {branch}) — "
                    "remove it manually."
                )
            return {
                "status": "applied",
                "files": applied_n,
                "conflicts": 0,
                "note": (
                    f"{applied_n} file(s) applied UNCOMMITTED to '{cur_branch}' in {repo_root} — "
                    f"{commit_hint}. {tail}"
                ),
            }

        # Some files conflicted: keep the worktree so they stay recoverable.
        clist = ", ".join(conflicts[:8]) + (" …" if len(conflicts) > 8 else "")
        if applied_n:
            return {
                "status": "partial",
                "files": applied_n,
                "conflicts": len(conflicts),
                "note": (
                    f"{applied_n} file(s) applied UNCOMMITTED to '{cur_branch}'; {commit_hint}. "
                    f"{len(conflicts)} file(s) CONFLICT with the current working tree (local "
                    f"edits or another agent's changes) and were NOT applied ({clist}) — resolve "
                    f"them from the retained worktree at {worktree_path} (branch {branch})."
                ),
            }
        return {
            "status": "kept",
            "files": 0,
            "conflicts": len(conflicts),
            "note": (
                f"all {len(conflicts)} changed file(s) CONFLICT with the current working tree "
                f"(local edits or another agent's changes) and were NOT applied ({clist}); they "
                f"remain in the worktree at {worktree_path} (branch {branch})."
            ),
        }
    except Exception as e:  # noqa: BLE001 — harvesting must never break the agent result
        log.warning("worktree harvest failed for %s: %s", worktree_path, e)
        return {
            "status": "error",
            "files": 0,
            "conflicts": 0,
            "note": f"harvest error ({e}); any changes remain at {worktree_path} (branch {branch}).",
        }
