"""Filesystem primitives, path validation, and shared constants.

Lowest layer of the workspace package, depends on nothing else inside it.
Owns the mutable backup dict so any caller (executors, routes, the approval
executors) mutates the same object regardless of how it was imported.
"""

import logging
import os
import stat
import tempfile
import unicodedata

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")


def _atomic_write_text(full: str, content: str) -> None:
    """Write ``content`` to ``full`` atomically as UTF-8 (see
    :func:`_atomic_write_bytes`)."""
    _atomic_write_bytes(full, content.encode("utf-8"))


def _atomic_write_bytes(full: str, data: bytes) -> None:
    """Write ``data`` to ``full`` atomically: write a unique sibling temp file
    (tempfile.mkstemp), fsync it, then os.replace it into place.

    The unique temp name means two concurrent writes to the same destination
    cannot clobber each other's partial write before the replace. Binary
    content round-trips byte for byte (no line-ending pass).

    Preserves the destination's existing permissions when overwriting; a new
    file gets the process umask default (matching a plain open()) instead of
    mkstemp's restrictive 0o600. A failed fsync or chmod fails the write: the
    caller is told, rather than getting a file that is not durable or carries
    the wrong mode.
    """
    parent = os.path.dirname(full) or "."
    os.makedirs(parent, exist_ok=True)
    basename = os.path.basename(full)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(os.stat(full).st_mode)
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{basename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if existing_mode is not None:
            target_mode = existing_mode
        else:
            # New file: match a plain open()'s umask-derived permissions
            # rather than mkstemp's 0o600, which would silently tighten
            # new files.
            umask = os.umask(0)
            os.umask(umask)
            target_mode = 0o666 & ~umask
        # Applied to the temp file so the atomic replace lands with the right
        # permissions (no window where ``full`` carries the wrong mode).
        os.chmod(tmp, target_mode)
        os.replace(tmp, full)
    except BaseException:
        # Remove the temp file on failure; the destination is untouched.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _resolve_path(path: str) -> str:
    """Resolve a path, handling Unicode normalization and trailing whitespace."""
    if os.path.isdir(path):
        return path
    parent = os.path.dirname(path)
    basename = os.path.basename(path)
    if not os.path.isdir(parent) or not basename:
        return path
    norm_base = unicodedata.normalize("NFC", basename)
    try:
        for entry in os.listdir(parent):
            norm_entry = unicodedata.normalize("NFC", entry.rstrip())
            if norm_entry == norm_base and os.path.isdir(os.path.join(parent, entry)):
                return os.path.join(parent, entry)
    except PermissionError:
        pass
    return path


DATA_DIR = data_root()
WORKSPACE_CONFIG_PATH = os.path.join(DATA_DIR, "workspace_config.json")
RECENT_WORKSPACES_PATH = os.path.join(DATA_DIR, "recent_workspaces.json")
# Directories written to via the one-shot save_file flow (server/workspace/
# state.py's record_save_location). Never 'connected' or 'indexed', this is
# purely so a file saved this way stays clickable/revealable via /open-with
# and /reveal afterward. See server/workspace/routes/os_integration.py.
SAVE_LOCATIONS_PATH = os.path.join(DATA_DIR, "save_locations.json")


def get_scratch_dir(session_id: str) -> str:
    """Per-session scratch directory for intermediate/throwaway files (temp
    scripts, probe files, format-conversion intermediates) that don't belong
    in the connected workspace. Created on first use; the caller is
    responsible for deleting anything it writes here once the task is done,
    this helper never cleans up on its own.
    """
    safe_id = session_id.strip() or "default"
    path = os.path.join(DATA_DIR, "scratch", safe_id)
    os.makedirs(path, exist_ok=True)
    return path


def own_scratch_dir(session_id: str | None) -> str | None:
    """The scratch folder this session may use (get_scratch_dir), or None
    without a session or when the id cannot name one.

    A session works only in its own folder, never in another session's (the
    user's rule). The id arrives in tool and approval payloads, so it must be
    one plain folder name: a separator, a leading dot (``..``) or a leading
    underscore (the ``_staging`` area) would reach outside that folder."""
    safe_id = (session_id or "").strip()
    if not safe_id or any(c in safe_id for c in ("/", "\\", "\0")) or safe_id[0] in "._":
        return None
    return get_scratch_dir(safe_id)


def in_own_scratch(filepath: str, session_id: str | None) -> bool:
    """True when ``filepath`` lies inside this session's own scratch folder."""
    own = own_scratch_dir(session_id)
    if own is None:
        return False
    real = os.path.realpath(filepath)
    root = os.path.realpath(own)
    return real == root or real.startswith(root + os.sep)


def in_other_scratch(filepath: str, session_id: str | None) -> bool:
    """True when ``filepath`` lies in the scratch area but outside this
    session's own folder: another session's folder, or the staging area."""
    real = os.path.realpath(filepath)
    root = os.path.realpath(scratch_root())
    if not (real == root or real.startswith(root + os.sep)):
        return False
    return not in_own_scratch(filepath, session_id)


# ── Save-flow staging ─────────────────────────────────────────────────
# File-saving tools build their full content BEFORE the approval card is
# shown: the finished bytes are written to this app-sandbox staging area at
# tool-call time, and approving just MOVES the ready file to the user's
# destination (server/approval/executors.py's _do_save_to_path). By the time
# the user sees the card, the file already exists and cannot fail to build.

_STAGING_MAX_AGE_SECONDS = 24 * 3600


def _staging_root() -> str:
    return os.path.join(DATA_DIR, "scratch", "_staging")


def stage_file_bytes(data: bytes, filename: str) -> str:
    """Write `data` to a fresh staging entry and return its absolute path.

    Each staged file gets its own random subdirectory so the original
    filename is preserved for the eventual move. Declined approvals leave
    their staged file behind (the approval pipeline never calls back on No),
    so every call also garbage-collects entries older than 24h, bounded,
    best-effort cleanup with no background task needed."""
    import shutil
    import time
    import uuid

    root = _staging_root()
    os.makedirs(root, exist_ok=True)

    cutoff = time.time() - _STAGING_MAX_AGE_SECONDS
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        try:
            if os.path.getmtime(full) < cutoff:
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            continue

    safe_name = os.path.basename(filename) or "file"
    entry_dir = os.path.join(root, uuid.uuid4().hex)
    os.makedirs(entry_dir)
    staged = os.path.join(entry_dir, safe_name)
    _atomic_write_bytes(staged, data)
    return staged


def is_staged_path(path: str) -> bool:
    """True only for paths inside the staging root. _do_save_to_path MUST
    check this before moving: the approval payload can in principle be
    forged via a direct POST /api/approval/execute, and without this gate a
    crafted staged_path could exfiltrate an arbitrary readable file by
    'moving' it into a user-visible folder."""
    real = os.path.realpath(path)
    root = os.path.realpath(_staging_root())
    return real.startswith(root + os.sep)


_WS_IGNORED_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    ".jj",
    ".sl",  # VCS directories
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    ".next",
    ".cache",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    ".coverage",
    "htmlcov",
    ".idea",
    ".vscode",
    "eggs",
    ".eggs",
    "target",
    ".terraform",
    ".serverless",
}
_WS_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".tiff",
    ".tif",
    ".pdf",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".flv",
    ".ogg",
    ".mkv",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".bz2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".whl",
    ".bin",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".ttf",
    ".woff",
    ".woff2",
    ".eot",
    ".sqlite",
    ".db",
    ".lock",
    ".jar",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}
# Image extensions that can be previewed in browser
_WS_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff", ".tif"}


def _normalize_lf(content: str) -> str:
    """Normalize line endings to LF."""
    return content.replace("\r\n", "\n").replace("\r", "\n")


_BLOCKED_PATH_PREFIXES = ("/dev/", "/proc/", "/sys/")


def _is_system_path(filepath: str) -> bool:
    """UNC paths (NTLM credential leaks) and the kernel pseudo-filesystems:
    never a valid file destination, whatever root is in play."""
    if filepath.startswith("\\\\") or filepath.startswith("//"):
        return True
    real = os.path.realpath(filepath)
    return any(real.startswith(p) or real == p.rstrip("/") for p in _BLOCKED_PATH_PREFIXES)


def _ws_validate_path(filepath: str, ws_root: str) -> bool:
    if _is_system_path(filepath):
        return False
    real = os.path.realpath(filepath)
    root = os.path.realpath(ws_root)
    return real == root or real.startswith(root + os.sep)


def scratch_root() -> str:
    """Parent of every per-session scratch directory (see get_scratch_dir)."""
    return os.path.join(DATA_DIR, "scratch")


def _ws_path_allowed(filepath: str, ws_root: str, session_id: str | None = None) -> bool:
    """Containment for the assistant's ws_* file tools and their approvals.

    Two roots are reachable: the connected workspace, and the calling
    session's own scratch folder (app-owned storage under the data root),
    where the system prompt sends throwaway files. The model is told to put
    probe scripts and intermediates there by absolute path, so the tools that
    create, read, edit and delete files must accept that path, or every probe
    stalls the turn. Without a session id no scratch folder is reachable.

    Other sessions' folders and the staging area stay out of reach (see
    own_scratch_dir and is_staged_path)."""
    if _ws_validate_path(filepath, ws_root):
        return True
    if _is_system_path(filepath):
        return False
    return in_own_scratch(filepath, session_id)


def ws_path_changeable(filepath: str, ws_root: str, session_id: str | None, bypass: bool) -> bool:
    """Whether a ws_* file tool may write, edit or delete ``filepath``.

    In no mode: a protected root (server.security.protected_roots: the
    filesystem root, a folder directly under it, the home folder or one above
    it), the same line as the rm -rf / hard block for commands. Outside
    bypassPermissions: the workspace and the session's own scratch folder
    (_ws_path_allowed). In bypassPermissions also anywhere else on the
    machine (the user's decision of 2026-11-02: Bypass works like Claude
    Code's bypass), except the kernel pseudo-filesystems, the scratch folders
    of other sessions, and the credential stores of the sensitive-path list,
    which a save outside the workspace refuses in every mode too."""
    from server.security.protected_roots import is_protected_root

    if is_protected_root(filepath):
        return False
    if _ws_path_allowed(filepath, ws_root, session_id):
        return True
    if not bypass or _is_system_path(filepath):
        return False
    from server.security.sensitive_paths import is_sensitive_path

    if is_sensitive_path(filepath):
        return False
    return not in_other_scratch(filepath, session_id)
