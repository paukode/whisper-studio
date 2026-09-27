"""JS/TS checks for the assistant with the workspace's own ESLint.

``check_file`` backs ``lsp_diagnostics`` for a JS/TS file. It runs
``<the app's node> <ws>/node_modules/eslint/bin/eslint.js --format json`` from
the project directory, so the project's ESLint version, config and plugins
apply. The json formatter ships with every ESLint (compact was removed from
core in ESLint 9). A workspace without ESLint, a missing node, or an ESLint
that fails to start (exit 2: no config, a broken plugin) is reported as the
tool's status, never as findings about the file.
"""

import json
import os

from .commands import (
    ToolUnavailable,
    eslint_config_file,
    find_workspace_eslint,
    first_error_line,
    missing_eslint_config_reason,
    node_path,
    run_tool,
)

ESLINT_TIMEOUT_S = 30.0
MAX_LISTED = 40

_NOT_A_FINDING = "This is the tool's status, not a finding about the file."


def check_file(ws: str, full_path: str, rel: str) -> str:
    install = find_workspace_eslint(ws, os.path.dirname(full_path))
    if install is None:
        return (
            f"ESLint is not available for {rel}: the workspace has no ESLint "
            "(node_modules/eslint) between this file and the workspace root. Add eslint and "
            f"an eslint.config.js to the project to get JS/TS checks. {_NOT_A_FINDING}"
        )
    node = node_path()
    if node is None:
        return (
            "ESLint is not available: Node.js was not found (the app runs the workspace's "
            f"ESLint with its own node). {_NOT_A_FINDING}"
        )
    try:
        run = run_tool(
            [node, install.entry, "--format", "json", full_path],
            cwd=install.project_dir,
            timeout=ESLINT_TIMEOUT_S,
        )
    except ToolUnavailable as e:
        return f"ESLint could not run ({e}). {_NOT_A_FINDING}"

    results = None
    if run.returncode in (0, 1):
        try:
            results = json.loads(run.stdout)
        except ValueError:
            results = None
    if not isinstance(results, list):
        file_dir = os.path.dirname(full_path)
        if eslint_config_file(install, file_dir) is None:
            # The usual exit 2. ESLint's own banner around it ends with a help
            # URL, so say it directly (and name a legacy config it ignores).
            reason = missing_eslint_config_reason(install, file_dir)
        else:
            reason = first_error_line(run.stderr) or first_error_line(run.stdout) or "no output"
        return (
            f"ESLint {install.version} could not lint {rel} (exit status {run.returncode}): "
            f"{reason}. {_NOT_A_FINDING}"
        )

    messages = [
        m
        for r in results
        if isinstance(r, dict)
        for m in r.get("messages") or []
        if isinstance(m, dict)
    ]
    if not messages:
        return f"ESLint {install.version}: no issues in {rel}."
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
    return f"ESLint {install.version}: {count} in {rel}:\n" + "\n".join(lines)
