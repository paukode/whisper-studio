"""GET /api/code-tools/status: what each code tool powers, whether it works
here, its version, and why not.

Every probe checks the same command the feature runs (see ``commands``), for
the connected workspace, so the Code tools page cannot report a tool the app
does not actually use. The app's own tools (ruff, pylsp, node,
typescript-language-server) are run with ``--version``, and ruff also checks an
empty file through the workspace's own config (``--no-fix``, like every
check-only run), so a config it rejects (a required-version, an unknown
setting) reads as not working, with its first error line. The workspace's
ESLint is not run: its version comes from its package.json, because opening a
settings page must never execute JavaScript from the workspace (a freshly
cloned repo may carry its own node_modules). lsp_diagnostics, which does run
it, reports an ESLint that fails to start. The route is a plain ``def``:
FastAPI runs it in the threadpool, so the probes' subprocesses never block the
event loop.
"""

import os
import shlex
import shutil
from dataclasses import asdict, dataclass

from fastapi import APIRouter

from . import ruff
from .commands import (
    SCAN_DEPTH,
    TSLS_INSTALL_HINT,
    ToolUnavailable,
    eslint_config_file,
    eslint_configs_below,
    eslint_install_in,
    first_error_line,
    missing_eslint_config_reason,
    node_path,
    parse_version,
    python_source_label,
    python_tool_argv,
    run_tool,
    workspace_dirs,
    workspace_ruff_config,
)

router = APIRouter(prefix="/api/code-tools", tags=["code-tools"])

PROBE_TIMEOUT_S = 10.0
# Folders whose ruff config the probe loads: the root and the first configs
# found below it (each governs the files beneath it).
MAX_RUFF_PROBES = 5


@dataclass
class ToolStatus:
    id: str
    name: str
    powers: str
    ok: bool
    version: str | None
    source: str
    command: str
    reason: str  # why it does not work and how to fix it; empty when ok
    note: str  # what else the user should know when it works


def _version_of(argv: list[str], cwd: str | None = None) -> str:
    """Run ``argv`` (a --version call) and return the version it reports.
    Raises ToolUnavailable with the reason when it does not run cleanly."""
    run = run_tool(argv, cwd=cwd, timeout=PROBE_TIMEOUT_S)
    if run.returncode != 0:
        raise ToolUnavailable(
            first_error_line(run.stderr) or f"it exited with status {run.returncode}"
        )
    version = parse_version(run.stdout) or parse_version(run.stderr)
    if not version:
        raise ToolUnavailable(f"it printed no version ({(run.stdout or run.stderr).strip()[:80]})")
    return version


def _cwd(ws: str | None) -> str | None:
    """Where the features run the Python tools: the workspace."""
    return ws if ws and os.path.isdir(ws) else None


def _listed(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} and {len(items) - limit} more" if len(items) > limit else shown


def _ruff_configs(ws: str) -> list[str]:
    """Every ruff config in the workspace (the root's first), relative to it:
    each one makes after_write fix and format the files beneath it."""
    root = os.path.realpath(ws)
    return [
        os.path.relpath(config, root)
        for d in workspace_dirs(ws)
        if (config := workspace_ruff_config(d, d))
    ]


def _ruff_check_probe(ws: str, folder: str) -> None:
    """Check an empty file in ``folder`` through ruff with the config that
    governs it, as the features do. Raises ToolUnavailable with ruff's reason
    when that config does not load."""
    argv = python_tool_argv(
        "ruff",
        "check",
        "--no-cache",
        "--no-fix",
        "--output-format",
        "json",
        "--stdin-filename",
        os.path.join(folder, "probe.py"),
        "-",
    )
    ruff.parse_findings(run_tool(argv, cwd=ws, timeout=PROBE_TIMEOUT_S, stdin=""))


def _ruff(ws: str | None) -> ToolStatus:
    argv = python_tool_argv("ruff")
    status = ToolStatus(
        id="ruff",
        name="Ruff",
        powers=(
            "Python checks for the assistant: lsp_diagnostics, and a check after every "
            "Python file it writes"
        ),
        ok=False,
        version=None,
        source=python_source_label(),
        command=shlex.join(argv),
        reason="",
        note="",
    )
    try:
        status.version = _version_of([*argv, "--version"], cwd=_cwd(ws))
        status.ok = True
    except ToolUnavailable as e:
        status.reason = f"ruff does not run in the app's Python: {e}."
        return status
    if not ws:
        status.note = "Connect a workspace to see whether ruff also fixes and formats after writes."
        return status
    root = os.path.realpath(ws)
    folders = [root, *(os.path.join(root, os.path.dirname(c)) for c in _ruff_configs(ws))]
    for folder in list(dict.fromkeys(folders))[:MAX_RUFF_PROBES]:
        try:
            _ruff_check_probe(ws, folder)
        except ToolUnavailable as e:
            where = os.path.relpath(folder, root)
            status.ok = False
            status.reason = (
                f"ruff {status.version} runs, but cannot check "
                f"{'files in this workspace' if where == '.' else f'files under {where}'} "
                f"with the project's ruff config: {e}."
            )
            return status
    if root_config := workspace_ruff_config(ws, ws):
        config_rel = os.path.relpath(root_config, os.path.realpath(ws))
        status.note = (
            f"This workspace configures ruff ({config_rel}), so after each write ruff also "
            "fixes what it can and formats the file."
        )
    elif configs := _ruff_configs(ws):
        dirs = [os.path.dirname(c) for c in configs]
        status.note = (
            f"The workspace root does not configure ruff, but {_listed(configs)} "
            f"{'does' if len(configs) == 1 else 'do'}: after a write under "
            f"{_listed(dirs)}, ruff also fixes what it can and formats the file. "
            "Elsewhere it only reports issues."
        )
    else:
        status.note = (
            "No ruff config was found in this workspace (pyproject.toml [tool.ruff], "
            f"ruff.toml or .ruff.toml, down to {SCAN_DEPTH} levels below the root), so after "
            "a write ruff only reports issues."
        )
    return status


def _pylsp(ws: str | None) -> ToolStatus:
    argv = python_tool_argv("pylsp")
    status = ToolStatus(
        id="pylsp",
        name="Python language server (pylsp)",
        powers="Python in the code editor: completion, hover and inline diagnostics",
        ok=False,
        version=None,
        source=python_source_label(),
        command=shlex.join(argv),
        reason="",
        note="",
    )
    try:
        status.version = _version_of([*argv, "--version"], cwd=_cwd(ws))
        status.ok = True
    except ToolUnavailable as e:
        status.reason = f"pylsp does not run in the app's Python: {e}."
    return status


def _eslint(ws: str | None) -> ToolStatus:
    status = ToolStatus(
        id="eslint",
        name="ESLint",
        powers="JS/TS checks for the assistant: lsp_diagnostics",
        ok=False,
        version=None,
        source="The workspace's node_modules",
        command="",
        reason="",
        note="",
    )
    if not ws:
        status.reason = "Connect a workspace: ESLint comes from the workspace's own node_modules."
        return status
    # The root's ESLint first; a monorepo may carry one per package instead.
    installs = [i for d in workspace_dirs(ws) if (i := eslint_install_in(d))]
    if not installs:
        status.reason = (
            "This workspace has no ESLint (no node_modules/eslint at its root or in a "
            "package). Add eslint and an eslint.config.js to the project to get JS/TS checks."
        )
        return status
    install = installs[0]
    root = os.path.realpath(ws)
    at_root = install.project_dir == root
    project_rel = os.path.relpath(install.project_dir, root)
    where = "" if at_root else f" in {project_rel}"
    node = node_path()
    status.command = shlex.join([node or "node", install.entry])
    if node is None:
        status.reason = "Node.js was not found, and the app runs the workspace's ESLint with it."
        return status
    # Read, never run: eslint_install_in took the version from the package's
    # package.json and checked that its CLI script exists.
    status.version = install.version or None
    label = f"ESLint {install.version}" if install.version else "ESLint"
    config = eslint_config_file(install)
    # ESLint 10 finds its config from each file's directory, so packages with
    # their own eslint.config.js get checks even when the project has none.
    below = [] if config else eslint_configs_below(install)
    if not config and not below:
        status.reason = (
            f"{label} is installed{where} but cannot lint: {missing_eslint_config_reason(install)}."
        )
        return status
    status.ok = True
    if config:
        status.note = f"Uses {config}."
    else:
        configs = [os.path.relpath(c, root) for c in below]
        dirs = [os.path.dirname(c) for c in configs]
        status.note = (
            f"No ESLint config covers {'the workspace root' if at_root else project_rel}, "
            f"but {_listed(configs)} {'does' if len(configs) == 1 else 'do'}: {label} reads "
            f"the config nearest each file, so files under {_listed(dirs)} get ESLint checks "
            "and files elsewhere do not."
        )
    try:
        status.note += f" Runs with Node {_version_of([node, '--version'])}."
    except ToolUnavailable:
        pass
    if not at_root:
        dirs = [os.path.relpath(i.project_dir, root) for i in installs]
        status.note += (
            f" The workspace root has no ESLint: only files under {_listed(dirs)} get "
            f"ESLint checks, and this row shows the one{where}."
        )
    return status


def _tsls() -> ToolStatus:
    found = shutil.which("typescript-language-server")
    status = ToolStatus(
        id="typescript-language-server",
        name="TypeScript language server",
        powers="JS/TS in the code editor: completion, hover and inline diagnostics",
        ok=False,
        version=None,
        source="Installed on this Mac (PATH)",
        command=shlex.join([found, "--stdio"]) if found else "",
        reason="",
        note="",
    )
    if not found:
        status.reason = TSLS_INSTALL_HINT
        return status
    try:
        status.version = _version_of([found, "--version"])
        status.ok = True
    except ToolUnavailable as e:
        status.reason = f"typescript-language-server does not start: {e}."
    return status


def status_report(ws: str | None) -> dict:
    tools = [_ruff(ws), _pylsp(ws), _eslint(ws), _tsls()]
    return {"workspace": ws or None, "tools": [asdict(t) for t in tools]}


@router.get("/status")
def code_tools_status():
    from server.workspace import get_workspace_path

    return status_report(get_workspace_path() or None)
