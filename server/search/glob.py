"""ws_glob executor — file path search powered by ripgrep.

Finds files matching glob patterns within the connected workspace.
Uses rg --files with --glob for fast, multi-threaded directory walking.
"""

import logging
import os

from server.executors import register_executor
from server.search.engine import (
    VCS_EXCLUDE_GLOBS,
    apply_pagination,
    pagination_hint,
    rg_raw,
    truncate_large_output,
)
from server.workspace import get_workspace_path

log = logging.getLogger("whisper-studio")

# Default file limit — configurable via WHISPER_GLOB_LIMIT env var.
_DEFAULT_GLOB_LIMIT = int(os.environ.get("WHISPER_GLOB_LIMIT", 500))


@register_executor("ws_glob", read_only=True, concurrent_safe=True)
def exec_ws_glob(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."

    pattern = tool_input.get("pattern", "")
    if not pattern:
        return "Error: pattern is required."

    head_limit = tool_input.get("head_limit")  # None=default, 0=unlimited
    offset = tool_input.get("offset", 0)

    args = _build_glob_args(pattern)

    try:
        stdout, stderr, returncode = rg_raw(args, cwd=ws)
    except Exception as e:
        return f"Search error: {e}"

    if returncode != 0 and stderr.strip():
        return f"Search error (exit {returncode}): {stderr.strip()}"

    files = stdout.rstrip("\n").split("\n") if stdout.strip() else []

    if not files:
        return "No files matched."

    paginated, truncated = apply_pagination(files, head_limit, _DEFAULT_GLOB_LIMIT, offset)

    result = "\n".join(paginated)
    if truncated:
        hint = pagination_hint(offset, len(paginated))
        result += f"\n{hint}"
    else:
        result += f"\n({len(paginated)} files)"
    return truncate_large_output(result)


def _build_glob_args(pattern: str) -> list[str]:
    """Build rg argument list for a glob (file listing) search."""
    args = [
        "--files",  # list files instead of searching content
        "--glob",
        pattern,  # apply glob pattern
        "--color",
        "never",
        "--sortr=modified",  # most recently modified first
    ]
    # Exclude VCS directories
    for excl in VCS_EXCLUDE_GLOBS:
        args.extend(["--glob", excl])
    return args
