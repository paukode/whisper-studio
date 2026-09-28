"""Ruff for the assistant's Python.

``check_file`` backs ``lsp_diagnostics`` for a Python file. ``after_write``
runs after every successful assistant write, create or edit of a Python file
in the workspace, and ``after_save`` after a Python file saved to an absolute
path (save_file, ws_create_file with a destination or with no workspace); each
returns a note for the tool result:

- ruff always checks the file and the model gets the findings;
- only when the workspace configures ruff (pyproject.toml [tool.ruff],
  ruff.toml or .ruff.toml) does ruff also fix what it can (``check --fix``)
  and format the file, and the note says exactly what changed. A file saved
  outside the connected workspace has no workspace to opt in, so it is only
  checked.

Everything runs with the app's own interpreter (``python -P -m ruff``), in the
workspace so the project's ruff config applies, with ``--no-cache`` (ruff
would otherwise write .ruff_cache into the project) and ``--force-exclude`` (a
file the project excludes stays untouched). Every check except the fix step
passes ``--no-fix``, so a ``fix = true`` in any config ruff resolves never
turns a check into an edit. A ruff that cannot run is said so in the result,
never reported as a clean file.
"""

import json
import os
import time
from collections import Counter
from dataclasses import dataclass

from .commands import (
    ToolRun,
    ToolUnavailable,
    first_error_line,
    python_tool_argv,
    run_tool,
    workspace_ruff_config,
)

RUFF_TIMEOUT_S = 10.0
# The whole post-write sequence (check, fix, format, re-check) shares one budget.
AFTER_WRITE_BUDGET_S = 20.0
MAX_LISTED = 40


@dataclass
class Finding:
    code: str
    message: str
    row: int
    column: int
    fixable: bool


def _check_argv(path: str, *, fix: bool = False) -> list[str]:
    """``ruff check`` on one file. A check-only run passes ``--no-fix``: ruff
    applies fixes by default when the config it resolves sets ``fix = true``,
    and that config can sit above the workspace or be the user's global one,
    so without the flag lsp_diagnostics (read-only, allowed in plan mode) or
    the check of an unconfigured workspace would rewrite the file."""
    args = ["check", "--no-cache", "--force-exclude", "--output-format", "json"]
    args.append("--fix" if fix else "--no-fix")
    return python_tool_argv("ruff", *args, path)


def _format_argv(path: str) -> list[str]:
    return python_tool_argv("ruff", "format", "--no-cache", "--force-exclude", path)


def _excluded(run: ToolRun) -> bool:
    """ruff skips an explicitly named file the project excludes (with
    --force-exclude) and says so on stderr."""
    return "No Python files found" in run.stderr


def parse_findings(run: ToolRun) -> list[Finding]:
    """Parse ``ruff check --output-format json``. Exit 0 means clean, 1 means
    findings; anything else, or output that is not the JSON list, means ruff
    itself failed (a bad config, a missing module) and raises."""
    if run.returncode not in (0, 1):
        reason = first_error_line(run.stderr) or f"ruff exited with status {run.returncode}"
        raise ToolUnavailable(reason)
    try:
        data = json.loads(run.stdout)
    except ValueError:
        reason = first_error_line(run.stderr) or "its output was not the expected JSON"
        raise ToolUnavailable(reason) from None
    if not isinstance(data, list):
        raise ToolUnavailable("its output was not the expected JSON")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        out.append(
            Finding(
                code=str(item.get("code") or "?"),
                message=str(item.get("message") or ""),
                row=int(loc.get("row") or 0),
                column=int(loc.get("column") or 0),
                fixable=item.get("fix") is not None,
            )
        )
    return out


def _render(findings: list[Finding], rel: str) -> str:
    lines = [
        f"  {rel}:{f.row}:{f.column}: {f.code} {f.message}" + (" (fixable)" if f.fixable else "")
        for f in findings[:MAX_LISTED]
    ]
    if len(findings) > MAX_LISTED:
        lines.append(f"  ... and {len(findings) - MAX_LISTED} more")
    return "\n".join(lines)


def _count(n: int) -> str:
    return f"{n} issue" if n == 1 else f"{n} issues"


def check_file(ws: str, full_path: str, rel: str) -> str:
    """lsp_diagnostics for a Python file."""
    try:
        run = run_tool(_check_argv(full_path), cwd=ws, timeout=RUFF_TIMEOUT_S)
        if _excluded(run):
            return f"ruff: {rel} is excluded by the project's ruff configuration, so it was not checked."
        findings = parse_findings(run)
    except ToolUnavailable as e:
        return f"Python diagnostics unavailable: ruff could not run ({e})."
    if not findings:
        return f"ruff: no issues in {rel}."
    return f"ruff: {_count(len(findings))} in {rel}:\n{_render(findings, rel)}"


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _fixed_summary(before: list[Finding], after: list[Finding]) -> str:
    fixed = Counter(f.code for f in before) - Counter(f.code for f in after)
    if not fixed:
        return ""
    total = sum(fixed.values())
    parts = ", ".join(f"{code} x{n}" if n > 1 else code for code, n in sorted(fixed.items()))
    return f"fixed {_count(total)} ({parts})"


def after_save(full_path: str, ws: str | None) -> str:
    """The note appended to the result of a Python file saved to an absolute
    path. Inside the connected workspace ``ws`` it is a write there like any
    other (the workspace's ruff config applies, fixes and formatting
    included); outside it ruff only checks the file, from its own folder."""
    real = os.path.realpath(full_path)
    if ws:
        root = os.path.realpath(ws)
        if os.path.commonpath([real, root]) == root:
            return after_write(ws, real, os.path.relpath(real, root))
    return after_write(os.path.dirname(real), real, os.path.basename(real), may_fix=False)


def after_write(ws: str, full_path: str, rel: str, *, may_fix: bool = True) -> str:
    """The note appended to the result of a successful write of a Python file.
    ``may_fix`` False (a file outside any workspace) only checks it."""
    deadline = time.monotonic() + AFTER_WRITE_BUDGET_S

    def budget() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolUnavailable(f"the checks did not finish within {AFTER_WRITE_BUDGET_S:g} s")
        return min(RUFF_TIMEOUT_S, remaining)

    config = workspace_ruff_config(ws, os.path.dirname(full_path)) if may_fix else None
    written = _read(full_path)
    try:
        first = run_tool(_check_argv(full_path), cwd=ws, timeout=budget())
        if _excluded(first):
            return (
                f"[ruff] {rel} is excluded by the project's ruff configuration, "
                "so it was not checked."
            )
        before = parse_findings(first)
        if config is None:
            why = (
                "This workspace does not configure ruff"
                if may_fix
                else "It was saved outside the connected workspace"
            )
            head = (
                f"[ruff] Checked {rel}. {why}, so ruff only checked the file; nothing was "
                "fixed or formatted."
            )
            if not before:
                return f"{head} No issues."
            return f"{head} {_count(len(before))}:\n{_render(before, rel)}"

        after_fix = parse_findings(
            run_tool(_check_argv(full_path, fix=True), cwd=ws, timeout=budget())
        )
        fixed_bytes = _read(full_path)
        fmt = run_tool(_format_argv(full_path), cwd=ws, timeout=budget())
        formatted = _read(full_path) != fixed_bytes
        if formatted:
            remaining = parse_findings(run_tool(_check_argv(full_path), cwd=ws, timeout=budget()))
        else:
            remaining = after_fix
    except ToolUnavailable as e:
        note = (
            f"[ruff] Could not check {rel}: ruff could not run ({e}). The write itself succeeded."
        )
        if config is not None and _read(full_path) != written:
            note += " ruff had already changed the file: re-read it before editing it again."
        return note

    config_rel = os.path.relpath(config, os.path.realpath(ws))
    fixed = _fixed_summary(before, after_fix)
    if not fixed and fixed_bytes != written:
        fixed = "applied fixes"
    changes = [c for c in (fixed, "reformatted" if formatted else "") if c]
    lines = [
        f"[ruff] This workspace configures ruff ({config_rel}), so after the write ruff "
        f"ran check --fix and format on {rel}.",
        f"Changed: {'; '.join(changes)}." if changes else "Changed: nothing.",
    ]
    if fmt.returncode != 0:
        reason = first_error_line(fmt.stderr) or f"exit status {fmt.returncode}"
        lines.append(f"Not formatted: ruff format failed ({reason}).")
    if _read(full_path) != written:
        lines.append(
            "The file on disk now differs from the content you wrote: re-read it before "
            "editing it again."
        )
    if remaining:
        lines.append(f"Remaining: {_count(len(remaining))}:\n{_render(remaining, rel)}")
    else:
        lines.append("Remaining: no issues.")
    return "\n".join(lines)
