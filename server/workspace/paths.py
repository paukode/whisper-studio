"""Filesystem primitives, path validation, and shared constants.

Lowest layer of the workspace package — depends on nothing else inside it.
Owns the mutable backup dict so any caller (executors, routes, the approval
bootstrap module) mutates the same object regardless of how it was imported.
"""

import logging
import os
import stat
import tempfile
import unicodedata

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")


def _atomic_write_text(full: str, content: str) -> None:
    """Write `content` to `full` atomically: write to a unique sibling temp
    file, fsync it, then os.replace into place.

    The temp file gets a UNIQUE name (via tempfile.mkstemp) rather than a
    per-process constant (`.name.tmp.<pid>`). Two concurrent writes to the same
    destination therefore use distinct temp files and cannot clobber each
    other's partial write before the atomic replace.

    Preserves the destination's existing permissions when overwriting; a new
    file gets the process umask default (matching a plain open()) instead of
    mkstemp's restrictive 0o600.
    """
    parent = os.path.dirname(full) or "."
    os.makedirs(parent, exist_ok=True)
    basename = os.path.basename(full)
    # Capture pre-existing mode so we can restore it after the replace
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(os.stat(full).st_mode)
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{basename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync not supported on all filesystems; non-fatal
                pass
        # Decide the destination mode, then apply it to the temp file so the
        # atomic replace lands with the right permissions (no window where
        # `full` briefly carries the wrong mode).
        if existing_mode is not None:
            target_mode: int | None = existing_mode
        else:
            # New file: match a plain open()'s umask-derived permissions rather
            # than mkstemp's 0o600, which would silently tighten new files.
            umask = os.umask(0)
            os.umask(umask)
            target_mode = 0o666 & ~umask
        if target_mode is not None:
            try:
                os.chmod(tmp, target_mode)
            except OSError as e:
                log.debug("could not set mode on %s: %s", tmp, e)
        os.replace(tmp, full)
    except Exception:
        # Best-effort cleanup of the temp file on failure
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


# Repo root lives three dirname() calls above this file:
#   server/workspace/paths.py -> server/workspace -> server -> <repo>
DATA_DIR = data_root()
WORKSPACE_CONFIG_PATH = os.path.join(DATA_DIR, "workspace_config.json")
RECENT_WORKSPACES_PATH = os.path.join(DATA_DIR, "recent_workspaces.json")
WORKSPACE_BACKUPS: dict[str, str] = {}

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


def _strip_trailing_ws(content: str, path: str) -> str:
    """Strip trailing whitespace per line, except for Markdown files."""
    if os.path.splitext(path)[1].lower() in (".md", ".mdx"):
        return content
    return "\n".join(line.rstrip() for line in content.split("\n"))


_BLOCKED_PATH_PREFIXES = ("/dev/", "/proc/", "/sys/")


def _ws_validate_path(filepath: str, ws_root: str) -> bool:
    # Block UNC paths to prevent NTLM credential leaks
    if filepath.startswith("\\\\") or filepath.startswith("//"):
        return False
    real = os.path.realpath(filepath)
    if any(real.startswith(p) or real == p.rstrip("/") for p in _BLOCKED_PATH_PREFIXES):
        return False
    root = os.path.realpath(ws_root)
    return real == root or real.startswith(root + os.sep)
