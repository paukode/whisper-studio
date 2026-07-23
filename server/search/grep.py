"""ws_grep executor — content search powered by ripgrep.

Provides regex-based file content search within the connected workspace.
Uses rg subprocess for fast, multi-threaded searching.

Output modes:
  files_with_matches — file paths only (default, lowest token cost)
  content            — matching lines with file:line:content format
  count              — per-file match counts
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

# Default match limit — configurable via WHISPER_GREP_LIMIT env var.
_DEFAULT_GREP_LIMIT = int(os.environ.get("WHISPER_GREP_LIMIT", 200))

# Valid output modes
_OUTPUT_MODES = {"files_with_matches", "content", "count"}


@register_executor("ws_grep", read_only=True, concurrent_safe=True)
def exec_ws_grep(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."

    pattern = tool_input.get("pattern", "")
    if not pattern:
        return "Error: pattern is required."

    output_mode = tool_input.get("output_mode", "files_with_matches")
    if output_mode not in _OUTPUT_MODES:
        return f"Error: output_mode must be one of {sorted(_OUTPUT_MODES)}"

    glob_filter = tool_input.get("glob", "")
    head_limit = tool_input.get("head_limit")  # None=default, 0=unlimited
    offset = tool_input.get("offset", 0)

    # Context lines (content mode only)
    context = tool_input.get("context")  # -C (supersedes before/after)
    context_before = tool_input.get("context_before")  # -B
    context_after = tool_input.get("context_after")  # -A

    case_sensitive = tool_input.get("case_sensitive", False)
    max_columns = tool_input.get("max_columns", 600)
    show_line_numbers = tool_input.get("show_line_numbers", True)
    file_type = tool_input.get("type", "")
    multiline = tool_input.get("multiline", False)

    args = _build_grep_args(
        pattern,
        glob_filter,
        output_mode,
        context=context,
        context_before=context_before,
        context_after=context_after,
        case_sensitive=case_sensitive,
        max_columns=max_columns,
        show_line_numbers=show_line_numbers,
        file_type=file_type,
        multiline=multiline,
    )

    try:
        stdout, stderr, returncode = rg_raw(args, cwd=ws)
    except Exception as e:
        return f"Search error: {e}"

    # rg exit code 1 = no matches (not an error)
    if returncode == 1:
        return _no_match_message(output_mode)
    if returncode != 0:
        return f"Search error (exit {returncode}): {stderr.strip()}"

    lines = stdout.rstrip("\n").split("\n") if stdout.strip() else []
    if not lines:
        return _no_match_message(output_mode)

    result = _format_output(lines, output_mode, head_limit, offset)
    return truncate_large_output(result)


def _no_match_message(output_mode: str) -> str:
    if output_mode == "files_with_matches":
        return "No matching files found."
    if output_mode == "count":
        return "No matches found. (0 total across 0 files)"
    return "No matches found."


def _format_output(lines: list[str], output_mode: str, head_limit: int | None, offset: int) -> str:
    """Apply pagination and format output based on mode."""
    total_before_pagination = len(lines)

    paginated, truncated = apply_pagination(lines, head_limit, _DEFAULT_GREP_LIMIT, offset)

    if output_mode == "files_with_matches":
        result = "\n".join(paginated)
        if truncated:
            hint = pagination_hint(offset, len(paginated))
            result += f"\n{hint}"
        else:
            result += f"\n({len(paginated)} files)"
        return result

    if output_mode == "count":
        # Sum match counts from file:N format
        total_matches = 0
        for line in lines:  # count from ALL lines, not just paginated
            parts = line.rsplit(":", 1)
            if len(parts) == 2 and parts[1].isdigit():
                total_matches += int(parts[1])

        result = "\n".join(paginated)
        if truncated:
            hint = pagination_hint(offset, len(paginated))
            result += (
                f"\n({total_matches} total matches across {total_before_pagination}+ files) {hint}"
            )
        else:
            result += f"\n({total_matches} total matches across {len(paginated)} files)"
        return result

    # content mode
    result = "\n".join(paginated)
    if truncated:
        hint = pagination_hint(offset, len(paginated))
        result += f"\n... {hint}"
    return result


def _build_grep_args(
    pattern: str,
    glob_filter: str,
    output_mode: str,
    *,
    context: int | None = None,
    context_before: int | None = None,
    context_after: int | None = None,
    case_sensitive: bool = False,
    max_columns: int = 600,
    show_line_numbers: bool = True,
    file_type: str = "",
    multiline: bool = False,
) -> list[str]:
    """Build rg argument list for a grep search."""
    args = [
        "--no-heading",  # flat output: file:line:match
        "--color",
        "never",
    ]

    # Exclude VCS directories
    for excl in VCS_EXCLUDE_GLOBS:
        args.extend(["--glob", excl])

    # Mode-specific flags
    if output_mode == "files_with_matches":
        args.append("-l")  # file paths only
        args.append("--sortr=modified")  # most recently modified first
    elif output_mode == "count":
        args.append("-c")  # per-file match counts
    else:
        # content mode
        if show_line_numbers:
            args.append("-n")  # line numbers
        if max_columns > 0:
            args.extend(["--max-columns", str(max_columns)])

        # Context lines (content mode only; -C supersedes -B/-A)
        if context is not None:
            args.extend(["-C", str(context)])
        else:
            if context_before is not None:
                args.extend(["-B", str(context_before)])
            if context_after is not None:
                args.extend(["-A", str(context_after)])

    # Case sensitivity
    if not case_sensitive:
        args.append("-i")

    # Multiline mode (dot matches newlines, patterns span lines)
    if multiline:
        args.extend(["-U", "--multiline-dotall"])

    # File type filter and glob filter
    # When both are specified, rg treats them as OR (additive inclusion).
    # Users expect AND semantics, so we resolve type extensions into glob
    # patterns combined with the directory glob.
    if file_type and glob_filter:
        type_globs = _resolve_type_globs(file_type)
        dir_globs = _parse_glob_patterns(glob_filter)
        if type_globs:
            for dg in dir_globs:
                for tg in type_globs:
                    # Combine: "server/**" + "*.py" → "server/**/*.py"
                    combined = f"{dg.rstrip('/')}/{tg}" if dg.endswith("**") else f"{dg}/{tg}"
                    args.extend(["--glob", combined])
        else:
            # Fallback: couldn't resolve type, use both separately
            args.extend(["--type", file_type])
            for g in dir_globs:
                args.extend(["--glob", g])
    elif file_type:
        args.extend(["--type", file_type])
    elif glob_filter:
        for g in _parse_glob_patterns(glob_filter):
            args.extend(["--glob", g])

    # Pattern (with leading-dash safety)
    if pattern.startswith("-"):
        args.extend(["-e", pattern])
    else:
        args.append(pattern)

    return args


def _resolve_type_globs(file_type: str) -> list[str]:
    """Resolve an rg file type name to its glob extensions via rg --type-list."""
    import subprocess

    from server.search.engine import get_rg_path

    try:
        result = subprocess.run(
            # Use the resolved ripgrep binary (env override / PATH / pip), the
            # same one every other rg invocation uses, rather than bare "rg"
            # which may not be on PATH in the server's environment.
            [get_rg_path(), "--type-list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith(f"{file_type}:"):
                # Format: "py: *.py, *.pyi"
                _, exts = line.split(":", 1)
                return [e.strip() for e in exts.split(",") if e.strip()]
    except Exception:
        pass
    return []


def _parse_glob_patterns(glob_str: str) -> list[str]:
    """Parse a glob string into individual patterns.

    Supports:
      - Comma-separated: "*.js,*.ts" → ["*.js", "*.ts"]
      - Space-separated: "*.js *.ts" → ["*.js", "*.ts"]
      - Brace-expanded: "*.{ts,tsx}" → ["*.{ts,tsx}"] (single pattern, rg expands)

    Brace patterns are never split on commas to avoid breaking the expansion.
    """
    patterns: list[str] = []
    # First split on whitespace
    raw_parts = glob_str.split()
    for raw in raw_parts:
        # If it contains braces, keep as single pattern (rg handles expansion)
        if "{" in raw and "}" in raw:
            patterns.append(raw)
        else:
            # Split on commas for non-brace patterns
            patterns.extend(p for p in raw.split(",") if p)
    return patterns
