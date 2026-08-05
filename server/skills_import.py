"""Git importer for folder skills.

Fetches selected skill folders (``<dir>/SKILL.md`` + ``scripts/`` / ``references/``
/ ``assets/``) from a git URL into the app's ``/skills/`` directory, using a
shallow, blobless, sparse checkout so a 300-skill mono-repo does not download
everything. Reuses the clone hygiene from ``server/git/executor.py`` and the
traversal-safe write discipline from the archived reference engine.

Two entry points:
  - ``preview(url)``  — cheap tree scan listing candidate skills (no blob fetch)
  - ``import_skills(url, subpaths, overwrite)`` — sparse-fetch, validate, copy
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time

from server import folder_skills
from server.git.executor import _GIT_URL_RE
from server.infrastructure.paths import skills_root

log = logging.getLogger("whisper-studio")

SKILLS_DIR = skills_root()

_CLONE_TIMEOUT_S = 300
_GIT_OP_TIMEOUT_S = 120
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB per file
_MAX_SKILL_BYTES = 25 * 1024 * 1024  # 25 MB per skill folder

# Local-folder upload limits (the "From local folder" import path). A folder
# picker can hand us a large tree, so cap both the file count and the aggregate
# size before we materialize anything on disk.
_MAX_UPLOAD_FILES = 3000
_MAX_UPLOAD_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB per upload

_ALLOWED_SUBDIRS = {"scripts", "references", "assets"}

# Names a folder skill must not claim — they collide with built-in tools.
_RESERVED_EXACT = {
    "terminal_run",
    "run_python",
    "aws_cli",
    "web_search",
    "web_fetch",
    "git_clone",
    "skill_list",
    "tool_search",
    "config_get",
    "list_agents",
    "spawn_subagent",
    "team_delete",
}
_RESERVED_PREFIXES = ("git_", "ws_", "task_", "cron_", "lsp_", "notebook_", "memory_", "mcp_")


class SkillImportError(Exception):
    """Raised for a recoverable import failure (bad URL, oversized folder, …)."""


def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    return env


def _run_git(
    args: list[str], cwd: str, timeout: int = _GIT_OP_TIMEOUT_S
) -> subprocess.CompletedProcess:
    from server.git.core import get_git_exe

    return subprocess.run(
        [get_git_exe(), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=_git_env(),
    )


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise SkillImportError("url is required")
    if not _GIT_URL_RE.match(url):
        raise SkillImportError(
            f"invalid url {url!r} — accepted forms are https://, ssh://, or "
            "git@host:path (no shell metacharacters, no file://)"
        )
    return url


def _clone_blobless(url: str, tmp: str) -> None:
    """Shallow, blobless clone with no working tree (trees only)."""
    try:
        r = _run_git(
            ["clone", "--no-checkout", "--depth", "1", "--filter=blob:none", "--", url, tmp],
            cwd=os.path.dirname(tmp),
            timeout=_CLONE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise SkillImportError("git clone timed out after 300s") from None
    if r.returncode != 0:
        raise SkillImportError(f"git clone failed: {(r.stderr or r.stdout or '').strip()}")


def _list_tree(tmp: str) -> list[str]:
    r = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp)
    if r.returncode != 0:
        raise SkillImportError(f"git ls-tree failed: {(r.stderr or r.stdout or '').strip()}")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _skill_dirs_from_tree(paths: list[str]) -> dict[str, list[str]]:
    """Map each skill directory (the parent of a SKILL.md) to the files under
    it. Top-level SKILL.md maps under the key ``""``."""
    dirs: dict[str, list[str]] = {}
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base in folder_skills.SKILL_MD_NAMES:
            d = p[: -len(base)].rstrip("/")
            dirs.setdefault(d, [])
    # attach member files
    for p in paths:
        for d in dirs:
            prefix = (d + "/") if d else ""
            if p.startswith(prefix) and (d == "" or p != d):
                dirs[d].append(p)
    return dirs


_MAX_DESC_CHARS = 400


def _fetch_descriptions(tmp: str, skill_dirs: list[str]) -> dict[str, str]:
    """Sparse-checkout only the SKILL.md files and parse their descriptions.

    Cheap (~1s even for a few hundred skills) because it fetches only the small
    SKILL.md blobs, not scripts/assets. Best-effort: returns {} on any failure so
    preview still works without descriptions."""
    out: dict[str, str] = {}
    try:
        _run_git(["sparse-checkout", "init", "--no-cone"], cwd=tmp)
        r = _run_git(
            ["sparse-checkout", "set", "**/SKILL.md", "SKILL.md", "**/skill.md", "skill.md"],
            cwd=tmp,
        )
        if r.returncode != 0:
            return out
        if _run_git(["checkout"], cwd=tmp).returncode != 0:
            return out
    except Exception as e:  # noqa: BLE001
        log.debug("description fetch failed: %s", e)
        return out
    for d in skill_dirs:
        for base in folder_skills.SKILL_MD_NAMES:
            p = os.path.join(tmp, d, base) if d else os.path.join(tmp, base)
            if os.path.isfile(p):
                try:
                    with open(p, errors="replace") as f:
                        fm, _ = folder_skills.parse_frontmatter(f.read())
                    out[d] = str(fm.get("description", "") or "").strip()[:_MAX_DESC_CHARS]
                except Exception:  # noqa: BLE001
                    pass
                break
    return out


def _build_skill_list(
    dirs: dict[str, list[str]], descriptions: dict[str, str], default_name: str
) -> list[dict]:
    """Turn a {skill_dir: member_paths} map plus a descriptions map into the
    preview payload shared by the git and local-folder import flows. ``default_name``
    names the ``""`` (repo/selection root) skill when one exists."""
    skills = []
    for d in sorted(dirs):
        members = dirs[d]
        scripts_prefix = (d + "/scripts/") if d else "scripts/"
        script_files = sorted(
            m.rsplit("/", 1)[-1]
            for m in members
            if m.startswith(scripts_prefix) and m.endswith(".py")
        )
        skills.append(
            {
                "subpath": d,
                "name": (d.rsplit("/", 1)[-1] if d else default_name),
                "description": descriptions.get(d, ""),
                "hasScripts": bool(script_files),
                "scriptFiles": script_files,
                "fileCount": len(members),
            }
        )
    return skills


def preview(url: str) -> dict:
    """List candidate skills in a repo, with descriptions, without downloading
    scripts or assets."""
    url = _validate_url(url)
    tmp = tempfile.mkdtemp(prefix="whisper_skillprev_")
    try:
        _clone_blobless(url, tmp)
        paths = _list_tree(tmp)
        dirs = _skill_dirs_from_tree(paths)
        descriptions = _fetch_descriptions(tmp, sorted(dirs))
        repo_base = url.rstrip("/").split("/")[-1]
        if repo_base.endswith(".git"):
            repo_base = repo_base[:-4]
        return {"url": url, "skills": _build_skill_list(dirs, descriptions, repo_base)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sparse_fetch(url: str, tmp: str, subpaths: list[str]) -> None:
    _clone_blobless(url, tmp)
    r = _run_git(["sparse-checkout", "init", "--cone"], cwd=tmp)
    if r.returncode != 0:
        raise SkillImportError(f"sparse-checkout init failed: {(r.stderr or '').strip()}")
    # Only pass in-repo relative dirs; "" (repo root) needs no set entry.
    setdirs = [s for s in subpaths if s]
    if setdirs:
        r = _run_git(["sparse-checkout", "set", *setdirs], cwd=tmp)
        if r.returncode != 0:
            raise SkillImportError(f"sparse-checkout set failed: {(r.stderr or '').strip()}")
    r = _run_git(["checkout"], cwd=tmp)
    if r.returncode != 0:
        raise SkillImportError(f"git checkout failed: {(r.stderr or r.stdout or '').strip()}")


def _safe_dest_name(folder_name: str) -> str | None:
    """Sanitize a source folder name into a destination directory name that
    stays inside SKILLS_DIR."""
    folder_name = (folder_name or "").strip()
    if (
        not folder_name
        or "/" in folder_name
        or "\\" in folder_name
        or os.sep in folder_name
        or ".." in folder_name
    ):
        return None
    dest = os.path.join(SKILLS_DIR, folder_name)
    root = os.path.realpath(SKILLS_DIR)
    real = os.path.realpath(dest)
    return dest if real.startswith(root + os.sep) else None


def _is_reserved(norm_name: str) -> bool:
    return norm_name in _RESERVED_EXACT or norm_name.startswith(_RESERVED_PREFIXES)


def _safe_write_file(src_file: str, dst_file: str) -> int:
    size = os.path.getsize(src_file)
    if size > _MAX_FILE_BYTES:
        raise SkillImportError(
            f"file exceeds {_MAX_FILE_BYTES} bytes: {os.path.basename(src_file)}"
        )
    with open(src_file, "rb") as f:
        data = f.read()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dst_file, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return size


def _copy_subtree(src: str, dst: str) -> int:
    """Recursively copy ``src`` into ``dst`` with traversal-safe writes,
    rejecting symlinks and dotfiles. Returns total bytes copied."""
    os.makedirs(dst, mode=0o700, exist_ok=True)
    total = 0
    for entry in sorted(os.listdir(src)):
        if entry.startswith("."):
            continue
        sp = os.path.join(src, entry)
        if os.path.islink(sp):
            continue
        dp = os.path.join(dst, entry)
        if os.path.isdir(sp):
            total += _copy_subtree(sp, dp)
        elif os.path.isfile(sp):
            total += _safe_write_file(sp, dp)
    return total


def _copy_skill_folder(src: str, dst: str) -> int:
    """Copy a skill folder: SKILL.md + top-level *.md + scripts/references/assets.
    Returns total bytes; raises if over the per-folder budget."""
    os.makedirs(dst, mode=0o700, exist_ok=False)
    total = 0
    for entry in sorted(os.listdir(src)):
        if entry.startswith("."):
            continue
        sp = os.path.join(src, entry)
        if os.path.islink(sp):
            continue
        if os.path.isfile(sp) and (entry in folder_skills.SKILL_MD_NAMES or entry.endswith(".md")):
            total += _safe_write_file(sp, os.path.join(dst, entry))
        elif os.path.isdir(sp) and entry in _ALLOWED_SUBDIRS:
            total += _copy_subtree(sp, os.path.join(dst, entry))
        if total > _MAX_SKILL_BYTES:
            raise SkillImportError(f"skill folder exceeds {_MAX_SKILL_BYTES} bytes")
    return total


def _import_one(tmp: str, subpath: str, overwrite: bool) -> dict:
    """Validate and copy a single skill folder. Returns a per-skill result."""
    src = os.path.realpath(os.path.join(tmp, subpath)) if subpath else os.path.realpath(tmp)
    tmp_root = os.path.realpath(tmp)
    if not (src == tmp_root or src.startswith(tmp_root + os.sep)) or not os.path.isdir(src):
        return {"subpath": subpath, "status": "error", "reason": "path escaped or missing"}
    if not folder_skills.is_folder_skill(src):
        return {"subpath": subpath, "status": "error", "reason": "no SKILL.md"}

    skill = folder_skills.load_folder_skill(src)
    if not skill or not skill.get("name"):
        return {"subpath": subpath, "status": "error", "reason": "unparseable SKILL.md"}
    if _is_reserved(skill["name"]):
        return {"subpath": subpath, "status": "error", "reason": f"reserved name: {skill['name']}"}

    folder_name = os.path.basename(src.rstrip(os.sep)) or skill["name"].replace("_", "-")
    dest = _safe_dest_name(folder_name)
    if not dest:
        return {"subpath": subpath, "status": "error", "reason": "invalid destination name"}

    if os.path.exists(dest):
        if not overwrite:
            return {
                "subpath": subpath,
                "name": skill["name"],
                "status": "conflict",
                "reason": "already exists",
            }
        backup = f"{dest}.bak-{int(time.time())}"
        shutil.move(dest, backup)

    try:
        _copy_skill_folder(src, dest)
    except SkillImportError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return {"subpath": subpath, "name": skill["name"], "status": "error", "reason": str(e)}
    return {
        "subpath": subpath,
        "name": skill["name"],
        "status": "imported",
        "hasScripts": skill["has_scripts"],
    }


def import_skills(url: str, subpaths: list[str], overwrite: bool = False) -> dict:
    """Sparse-fetch the given subpaths from ``url`` and copy valid skill folders
    into SKILLS_DIR. Returns {imported, conflicts, errors} lists. The caller is
    responsible for reloading the skill registry afterwards."""
    url = _validate_url(url)
    subpaths = [s.strip().strip("/") for s in (subpaths or []) if s is not None]
    if not subpaths:
        raise SkillImportError("no skills selected")
    os.makedirs(SKILLS_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="whisper_skillimport_")
    results: list[dict] = []
    try:
        _sparse_fetch(url, tmp, subpaths)
        for sp in subpaths:
            results.append(_import_one(tmp, sp, overwrite))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {
        "imported": [r for r in results if r["status"] == "imported"],
        "conflicts": [r for r in results if r["status"] == "conflict"],
        "errors": [r for r in results if r["status"] == "error"],
    }


# --- Local-folder import (uploaded tree; no git) ---------------------------
#
# The "From local folder" UI hands us the picked directory as a flat list of
# ``(relative_path, bytes)`` pairs (each ``webkitRelativePath`` + its content).
# We rebuild that tree in a tempdir with the same traversal-safe write
# discipline the git path uses, then reuse the identical detection/validation/
# copy helpers (``_skill_dirs_from_tree`` / ``_import_one`` / ``_copy_skill_folder``
# / ``_safe_dest_name`` / ``_is_reserved``). Nothing here trusts the client's
# paths: ``_safe_rel`` rejects absolute paths and ``..`` escapes before a byte
# is written, and every write goes through an O_EXCL|O_NOFOLLOW open under the
# tempdir root.


def _safe_rel(relpath: str) -> str | None:
    """Normalize a client-supplied relative path to a POSIX path that stays
    under the tempdir root, or None if it is absolute, empty, or contains a
    ``..`` / drive-letter component."""
    rel = (relpath or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/"):
        return None
    parts: list[str] = []
    for i, seg in enumerate(rel.split("/")):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        # Reject a Windows drive-letter first segment (``C:``).
        if i == 0 and len(seg) == 2 and seg[1] == ":":
            return None
        parts.append(seg)
    if not parts:
        return None
    return "/".join(parts)


def _reconstruct_tree(files: list[tuple[str, bytes]], tmp: str) -> list[str]:
    """Materialize ``files`` under ``tmp`` with traversal-safe writes. Returns
    the list of written POSIX relpaths. Raises SkillImportError on any unsafe
    path, an oversized file, or an oversized/too-large aggregate upload."""
    files = files or []
    if len(files) > _MAX_UPLOAD_FILES:
        raise SkillImportError(f"too many files (> {_MAX_UPLOAD_FILES})")
    root = os.path.realpath(tmp)
    total = 0
    written: list[str] = []
    for relpath, data in files:
        safe = _safe_rel(relpath)
        if not safe:
            raise SkillImportError(f"unsafe path in upload: {relpath!r}")
        if len(data) > _MAX_FILE_BYTES:
            raise SkillImportError(f"file exceeds {_MAX_FILE_BYTES} bytes: {safe}")
        total += len(data)
        if total > _MAX_UPLOAD_TOTAL_BYTES:
            raise SkillImportError(f"upload exceeds {_MAX_UPLOAD_TOTAL_BYTES} bytes")
        dest = os.path.join(root, *safe.split("/"))
        parent = os.path.dirname(dest)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        real_parent = os.path.realpath(parent)
        if not (real_parent == root or real_parent.startswith(root + os.sep)):
            raise SkillImportError(f"path escaped tempdir: {safe}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(dest, flags, 0o600)
        except FileExistsError:
            # Duplicate relpath in the upload — keep the first, ignore repeats.
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        written.append(safe)
    return written


def _local_descriptions(tmp: str, skill_dirs: list[str]) -> dict[str, str]:
    """Parse the ``description`` frontmatter from each reconstructed SKILL.md.
    Best-effort: a skill with an unreadable/unparseable SKILL.md just gets an
    empty description (same contract as the git preview)."""
    out: dict[str, str] = {}
    for d in skill_dirs:
        for base in folder_skills.SKILL_MD_NAMES:
            p = os.path.join(tmp, d, base) if d else os.path.join(tmp, base)
            if os.path.isfile(p):
                try:
                    with open(p, errors="replace") as f:
                        fm, _ = folder_skills.parse_frontmatter(f.read())
                    out[d] = str(fm.get("description", "") or "").strip()[:_MAX_DESC_CHARS]
                except Exception:  # noqa: BLE001
                    pass
                break
    return out


def preview_local(files: list[tuple[str, bytes]]) -> dict:
    """List candidate skills in an uploaded local folder tree, mirroring
    ``preview()``'s payload shape so the UI can reuse the same select→import UX."""
    tmp = tempfile.mkdtemp(prefix="whisper_skillprevlocal_")
    try:
        paths = _reconstruct_tree(files, tmp)
        dirs = _skill_dirs_from_tree(paths)
        descriptions = _local_descriptions(tmp, sorted(dirs))
        return {"skills": _build_skill_list(dirs, descriptions, "skill")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_local(
    files: list[tuple[str, bytes]], subpaths: list[str], overwrite: bool = False
) -> dict:
    """Rebuild the uploaded tree and copy the selected skill folders into
    SKILLS_DIR. Returns {imported, conflicts, errors}. The caller reloads the
    skill registry afterwards (identical to the git path)."""
    subpaths = [s.strip().strip("/") for s in (subpaths or []) if s is not None]
    if not subpaths:
        raise SkillImportError("no skills selected")
    os.makedirs(SKILLS_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="whisper_skillimportlocal_")
    results: list[dict] = []
    try:
        available = set(_reconstruct_tree(files, tmp))
        dirs = set(_skill_dirs_from_tree(sorted(available)))
        for sp in subpaths:
            # Only import subpaths the upload actually offered — never let a
            # client name a folder that was not part of the tree it uploaded.
            if sp not in dirs:
                results.append(
                    {"subpath": sp, "status": "error", "reason": "not in uploaded folder"}
                )
                continue
            results.append(_import_one(tmp, sp, overwrite))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {
        "imported": [r for r in results if r["status"] == "imported"],
        "conflicts": [r for r in results if r["status"] == "conflict"],
        "errors": [r for r in results if r["status"] == "error"],
    }
