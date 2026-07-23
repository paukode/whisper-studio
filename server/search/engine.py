"""Ripgrep search engine — binary detection and low-level subprocess interface.

Detects the rg binary once at import time (memoized). Provides raw functions
to invoke rg for grep and glob operations. All higher-level logic (output
modes, pagination, sorting) lives in grep.py and glob.py.
"""

import logging
import os
import shutil
import subprocess
from functools import lru_cache

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# VCS directories to exclude from search (noise and slow to traverse)
VCS_EXCLUDE_GLOBS = [
    "!.git",
    "!.svn",
    "!.hg",
    "!.bzr",
    "!.jj",
    "!.sl",
]

# Persist threshold: results larger than this are saved to disk
_PERSIST_THRESHOLD_BYTES = 20_000
_SEARCH_OUTPUT_DIR = os.path.join(data_root(), "search_output")

# ---------------------------------------------------------------------------
# Ripgrep binary detection (memoized — checked once per process)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_rg_path() -> str:
    """Return the absolute path to the rg binary, or raise if not found.

    Resolution order:
      1. WHISPER_RG_PATH environment variable (explicit override)
      2. shutil.which("rg") — find on PATH
      3. pip-installed ripgrep package binary location

    Raises RuntimeError if ripgrep is not available.
    """
    # 1. Explicit override
    env_path = os.environ.get("WHISPER_RG_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        log.info("ripgrep: using WHISPER_RG_PATH=%s", env_path)
        return env_path

    # 2. System PATH
    system_rg = shutil.which("rg")
    if system_rg:
        log.info("ripgrep: found on PATH at %s", system_rg)
        return system_rg

    # 3. pip-installed ripgrep package
    try:
        import ripgrep as _rg_pkg  # noqa: F401

        pip_rg = shutil.which("rg")  # should now be on PATH after pip install
        if pip_rg:
            log.info("ripgrep: found via pip package at %s", pip_rg)
            return pip_rg
    except ImportError:
        pass

    raise RuntimeError(
        "ripgrep (rg) is required but not found. "
        "Install via: brew install ripgrep  OR  pip install ripgrep"
    )


def has_ripgrep() -> bool:
    """Check if ripgrep is available (does not raise)."""
    try:
        get_rg_path()
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Low-level ripgrep invocation
# ---------------------------------------------------------------------------

# Default timeout in seconds; can be overridden via WHISPER_SEARCH_TIMEOUT
_DEFAULT_TIMEOUT = 20


def _get_timeout() -> int:
    return int(os.environ.get("WHISPER_SEARCH_TIMEOUT", _DEFAULT_TIMEOUT))


# ---------------------------------------------------------------------------
# Pagination utility (shared by grep and glob)
# ---------------------------------------------------------------------------


def apply_pagination(
    items: list[str],
    head_limit: int | None,
    default_limit: int,
    offset: int = 0,
) -> tuple[list[str], bool]:
    """Slice items by offset and head_limit, returning (sliced, was_truncated).

    Args:
        items: Full list of result lines/paths.
        head_limit: Caller-requested limit. None = use default, 0 = unlimited.
        default_limit: Fallback when head_limit is None.
        offset: Number of items to skip from the front.

    Returns:
        (sliced_items, was_truncated) — truncated is True when more results exist.
    """
    if offset > 0:
        items = items[offset:]

    if head_limit == 0:
        # Explicit unlimited
        return items, False

    effective = head_limit if head_limit is not None else default_limit
    if len(items) > effective:
        return items[:effective], True
    return items, False


def truncate_large_output(output: str) -> str:
    """If output exceeds persist threshold, save full version to disk and truncate.

    Returns the output unchanged if under threshold, or a truncated version
    with head + tail + reference to the persisted file.
    """
    if len(output.encode("utf-8", errors="replace")) <= _PERSIST_THRESHOLD_BYTES:
        return output

    import time

    os.makedirs(_SEARCH_OUTPUT_DIR, exist_ok=True)
    filename = f"search_{int(time.time() * 1000)}.txt"
    filepath = os.path.join(_SEARCH_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w") as f:
            f.write(output)
    except Exception:
        filepath = "(failed to persist)"

    lines = output.split("\n")
    head = "\n".join(lines[:50])
    tail = "\n".join(lines[-20:])
    return (
        f"{head}\n\n"
        f"... ({len(output)} chars, {len(lines)} lines total — truncated for token efficiency) ...\n\n"
        f"{tail}\n"
        f"Full output saved to: {filepath}"
    )


def pagination_hint(offset: int, limit_used: int) -> str:
    """Build a paging hint string for truncated results."""
    next_offset = offset + limit_used
    return f"(showing {limit_used} results, use offset={next_offset} to see next page)"


_SIGKILL_DELAY = 5  # seconds to wait after SIGTERM before SIGKILL


def _is_eagain_error(stderr: str) -> bool:
    """Check if rg failed due to EAGAIN (resource temporarily unavailable)."""
    return "os error 11" in stderr or "Resource temporarily unavailable" in stderr


def rg_raw(
    args: list[str], cwd: str, timeout: int | None = None, *, _is_retry: bool = False
) -> tuple[str, str, int]:
    """Run rg with the given arguments and return (stdout, stderr, returncode).

    Args:
        args: Arguments to pass to rg (not including the rg binary itself).
        cwd: Working directory for the rg process.
        timeout: Timeout in seconds. Defaults to WHISPER_SEARCH_TIMEOUT or 20s.

    Returns:
        Tuple of (stdout, stderr, returncode).

    Raises:
        subprocess.TimeoutExpired: If the process exceeds the timeout after
        both SIGTERM and SIGKILL escalation.
    """
    rg_path = get_rg_path()
    timeout = timeout or _get_timeout()

    cmd = [rg_path] + args

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)

        # EAGAIN retry: if rg failed due to resource exhaustion, retry single-threaded
        if proc.returncode != 0 and not _is_retry and _is_eagain_error(stderr):
            log.info("rg EAGAIN error, retrying with -j 1 (single-threaded)")
            return rg_raw(["-j", "1"] + args, cwd, timeout, _is_retry=True)

        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        # Graceful shutdown: SIGTERM first
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=_SIGKILL_DELAY)
            log.warning("rg process terminated via SIGTERM after %ds timeout", timeout)
            return stdout or "", stderr or "", proc.returncode
        except subprocess.TimeoutExpired:
            # Force kill if SIGTERM didn't work
            proc.kill()
            proc.wait()
            log.warning("rg process killed via SIGKILL after SIGTERM failed")
            raise subprocess.TimeoutExpired(
                cmd, timeout, output="", stderr="Search timed out and was force-killed"
            ) from None
