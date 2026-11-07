"""Prettier, then ESLint --fix, on a JS/TS file the assistant just wrote.

The JS/TS side of server/code_tools/after_write.py. Each tool runs only when
the workspace sets it up: its own package in node_modules between the file and
the workspace root, and a config for it (an eslint.config.* ESLint reads, a
.prettierrc or another Prettier config). Both run with the app's node, from
the directory that holds the package, so the project's versions, configs and
ignore files apply. Prettier goes first, so the ESLint findings reported are
those of the file as it is left.
"""

import json
import os

from .commands import (
    ToolUnavailable,
    eslint_config_file,
    find_workspace_eslint,
    find_workspace_prettier,
    first_error_line,
    node_path,
    run_tool,
    workspace_prettier_config,
)

JS_FIX_TIMEOUT_S = 30.0
MAX_LISTED = 40


def after_write(ws: str, full_path: str, rel: str) -> str:
    """The note for the model: what each configured tool did not fix, or why
    it could not run. Empty when neither tool is set up or nothing is left."""
    node = node_path()
    if node is None:
        return ""
    file_dir = os.path.dirname(full_path)
    notes = []
    prettier = find_workspace_prettier(ws, file_dir)
    if prettier and workspace_prettier_config(ws, file_dir):
        try:
            run = run_tool(
                [node, prettier.entry, "--write", full_path],
                cwd=prettier.project_dir,
                timeout=JS_FIX_TIMEOUT_S,
            )
            if run.returncode != 0:
                reason = first_error_line(run.stderr) or f"exit status {run.returncode}"
                notes.append(f"[prettier] Could not format {rel}: {reason}.")
        except ToolUnavailable as e:
            notes.append(f"[prettier] Could not format {rel}: Prettier could not run ({e}).")
    eslint = find_workspace_eslint(ws, file_dir)
    if eslint and eslint_config_file(eslint, file_dir):
        notes.append(_eslint_fix(node, eslint, full_path, rel))
    return "\n".join(n for n in notes if n)


def _eslint_fix(node: str, install, full_path: str, rel: str) -> str:
    try:
        run = run_tool(
            [node, install.entry, "--fix", "--format", "json", full_path],
            cwd=install.project_dir,
            timeout=JS_FIX_TIMEOUT_S,
        )
    except ToolUnavailable as e:
        return f"[eslint] Could not check {rel}: ESLint could not run ({e})."
    results = None
    if run.returncode in (0, 1):
        try:
            results = json.loads(run.stdout)
        except ValueError:
            results = None
    if not isinstance(results, list):
        reason = first_error_line(run.stderr) or first_error_line(run.stdout) or "no output"
        return f"[eslint] Could not check {rel} (exit status {run.returncode}): {reason}."
    # A file the project's ignores exclude comes back as one rule-less
    # warning; it is not a finding about the code.
    messages = [
        m
        for r in results
        if isinstance(r, dict)
        for m in r.get("messages") or []
        if isinstance(m, dict) and not str(m.get("message", "")).startswith("File ignored")
    ]
    if not messages:
        return ""
    lines = []
    for m in messages[:MAX_LISTED]:
        severity = "error" if m.get("severity") == 2 else "warning"
        rule = f" ({m['ruleId']})" if m.get("ruleId") else ""
        lines.append(
            f"  {rel}:{m.get('line') or 0}:{m.get('column') or 0}: {severity} "
            f"{m.get('message') or ''}{rule}"
        )
    if len(messages) > MAX_LISTED:
        lines.append(f"  ... and {len(messages) - MAX_LISTED} more")
    count = f"{len(messages)} issue" if len(messages) == 1 else f"{len(messages)} issues"
    return f"[eslint] {count} left in {rel} after --fix:\n" + "\n".join(lines)
