"""How the app launches each code tool.

- Python tools (ruff, pylsp) run as ``<the app's python> -P -m <module>``:
  the bundled interpreter in the Mac app, the venv's in a source install.
  Never a PATH lookup or a console script: the scripts in the bundle carry the
  build machine's interpreter in their shebang, so they fail on every other
  Mac. ``-P`` matters because every caller runs in the workspace: plain
  ``-m`` puts the working directory first on sys.path, so a workspace
  ``ruff.py`` or ``logging.py`` would run in the app's interpreter instead of
  the tool. Not ``-I``, which would also drop PYTHONPYCACHEPREFIX and let the
  bundled runtime write .pyc files into the signed bundle.
- ESLint is the workspace's own package, run with the app's node
  (``WHISPER_NODE_PATH`` in the Mac app, the venv's node in a source install).
  Never npx, which can download packages from the internet.
- typescript-language-server ships with neither install. It is looked up on
  PATH, and when it is missing that is reported, not hidden.
"""

import collections
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import tomllib

from server.infrastructure.binaries import resolve

PYTHON_EXTENSIONS = (".py", ".pyi")
JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

# The files that make ruff "configured by the project", in ruff's own order of
# precedence within one directory. A pyproject.toml only counts with a
# [tool.ruff] table, exactly as ruff treats it.
RUFF_CONFIG_FILES = (".ruff.toml", "ruff.toml", "pyproject.toml")

# ESLint's flat config, the only kind ESLint 9 and 10 read by default, in
# ESLint's own order of precedence within one directory.
FLAT_ESLINT_CONFIG_FILES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
)
# The legacy eslintrc files (plus package.json eslintConfig): ESLint 8 reads
# them, ESLint 9 only with ESLINT_USE_FLAT_CONFIG=false, ESLint 10 never.
LEGACY_ESLINT_CONFIG_FILES = (
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.yaml",
    ".eslintrc.yml",
    ".eslintrc.json",
    ".eslintrc",
)

# The status page looks this far below the workspace root for subprojects
# with their own ruff config or ESLint, and visits at most this many
# directories, so a huge tree cannot stall it.
SCAN_DEPTH = 3
SCAN_MAX_DIRS = 2000

TSLS_INSTALL_HINT = (
    "typescript-language-server is not installed. The app does not ship it: install it "
    "with `npm install -g typescript-language-server typescript`, then reopen the file."
)


class ToolUnavailable(Exception):
    """A tool could not be started or did not finish. The message says why, in
    words fit to show the user or the model."""


@dataclass
class ToolRun:
    returncode: int
    stdout: str
    stderr: str


def is_packaged() -> bool:
    """True inside the Mac app: the shell always sets WHISPER_HOME."""
    return bool(os.environ.get("WHISPER_HOME", "").strip())


def python_tool_argv(module: str, *args: str) -> list[str]:
    """``<the app's python> -P -m <module> ...``: safe-path mode, so the
    working directory (the workspace) never shadows the tool's imports."""
    return [sys.executable, "-P", "-m", module, *args]


def python_source_label() -> str:
    return "Bundled with the app" if is_packaged() else "The app's Python environment"


def node_path() -> str | None:
    """The node the app runs ESLint with: the bundled one in the Mac app
    (WHISPER_NODE_PATH), otherwise the first node on PATH (the venv's in a
    source install, where setup.sh puts it)."""
    return resolve("node", "WHISPER_NODE_PATH")


def run_tool(
    argv: list[str], *, cwd: str | None = None, timeout: float = 10.0, stdin: str | None = None
) -> ToolRun:
    """Run a tool to completion, with ``stdin`` as its input when given.
    Raises ToolUnavailable when it cannot start or runs past ``timeout``; a
    non-zero exit is returned for the caller to read."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ToolUnavailable(f"{argv[0]} was not found") from None
    except subprocess.TimeoutExpired:
        raise ToolUnavailable(f"it did not finish within {timeout:g} s") from None
    except OSError as e:
        raise ToolUnavailable(str(e)) from None
    return ToolRun(proc.returncode, proc.stdout or "", proc.stderr or "")


# A line that states a failure: it starts with "error"/"fatal" or an exception
# name ("TypeError: ...", "Error: Cannot find module ..."), or it says anywhere
# that something is missing ("ESLint couldn't find an eslint.config.(js|mjs|cjs)
# file.", "...: No module named ruff").
_FAILURE_START = re.compile(r"(?i:error|fatal)\b|\w*(Error|Exception)\b")
_MISSING = re.compile(
    r"No module named|couldn't find|could not find|cannot find|No such file", re.IGNORECASE
)


def first_error_line(text: str) -> str:
    """The line of a tool's output that says why it failed, for a one-line
    reason: the first line that states a failure, else the last line. Returned
    without a trailing period, since callers put it inside a sentence."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    stated = (ln for ln in lines if _FAILURE_START.match(ln) or _MISSING.search(ln))
    line = next(stated, lines[-1] if lines else "")
    return line.rstrip(".").rstrip()


def parse_version(text: str) -> str | None:
    m = re.search(r"v?(\d+(?:\.\d+)+)", text or "")
    return m.group(1) if m else None


def _dirs_up_to(start_dir: str, root: str) -> Iterator[str]:
    """``start_dir`` and each parent up to and including ``root``. Nothing when
    ``start_dir`` is not inside ``root``."""
    root_real = os.path.realpath(root)
    d = os.path.realpath(start_dir)
    if d != root_real and not d.startswith(root_real + os.sep):
        return
    while True:
        yield d
        if d == root_real:
            return
        d = os.path.dirname(d)


def _configures_ruff(path: str) -> bool:
    if os.path.basename(path) != "pyproject.toml":
        return True
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool = data.get("tool")
    return isinstance(tool, dict) and "ruff" in tool


def workspace_ruff_config(ws: str, start_dir: str) -> str | None:
    """The config file through which the workspace configures ruff for files in
    ``start_dir``: the nearest .ruff.toml, ruff.toml, or pyproject.toml with a
    [tool.ruff] table, looking from ``start_dir`` up to the workspace root. A
    config above the workspace does not count."""
    for d in _dirs_up_to(start_dir, ws):
        for name in RUFF_CONFIG_FILES:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and _configures_ruff(candidate):
                return candidate
    return None


def workspace_dirs(ws: str) -> Iterator[str]:
    """The workspace root, then its subdirectories breadth first down to
    SCAN_DEPTH levels, at most SCAN_MAX_DIRS in all. Skips hidden directories,
    node_modules, __pycache__ and virtualenvs (a pyvenv.cfg), and never
    follows a symlink out of the tree."""
    queue = collections.deque([(os.path.realpath(ws), 0)])
    visited = 0
    while queue and visited < SCAN_MAX_DIRS:
        d, depth = queue.popleft()
        visited += 1
        yield d
        if depth >= SCAN_DEPTH:
            continue
        try:
            with os.scandir(d) as entries:
                names = sorted(
                    e.name
                    for e in entries
                    if e.is_dir(follow_symlinks=False)
                    and not e.name.startswith(".")
                    and e.name not in ("node_modules", "__pycache__")
                )
        except OSError:
            continue
        for name in names:
            sub = os.path.join(d, name)
            if not os.path.isfile(os.path.join(sub, "pyvenv.cfg")):
                queue.append((sub, depth + 1))


@dataclass
class EslintInstall:
    project_dir: str  # the directory that holds node_modules/eslint; ESLint's cwd
    entry: str  # ESLint's CLI script, run with the app's node
    version: str


def eslint_install_in(d: str) -> EslintInstall | None:
    """The ESLint installed in ``d``/node_modules, if any."""
    pkg_dir = os.path.join(d, "node_modules", "eslint")
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    bin_field = meta.get("bin")
    if isinstance(bin_field, dict):
        rel = bin_field.get("eslint") or ""
    elif isinstance(bin_field, str):
        rel = bin_field
    else:
        rel = ""
    entry = os.path.normpath(os.path.join(pkg_dir, rel or "bin/eslint.js"))
    if not os.path.isfile(entry):
        return None
    return EslintInstall(project_dir=d, entry=entry, version=str(meta.get("version", "")))


def find_workspace_eslint(ws: str, start_dir: str) -> EslintInstall | None:
    """The workspace's own ESLint nearest to ``start_dir`` (a monorepo package
    may carry its own), looking up to the workspace root."""
    for d in _dirs_up_to(start_dir, ws):
        if install := eslint_install_in(d):
            return install
    return None


def _dirs_to_fs_root(start_dir: str) -> Iterator[str]:
    d = os.path.realpath(start_dir)
    while True:
        yield d
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def _flat_eslint_config_in(d: str) -> str | None:
    for name in FLAT_ESLINT_CONFIG_FILES:
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


def _legacy_eslint_config_in(d: str) -> str | None:
    for name in LEGACY_ESLINT_CONFIG_FILES:
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(meta, dict) and "eslintConfig" in meta:
        return os.path.join(d, "package.json")
    return None


def _nearest(find_in, start_dir: str) -> str | None:
    return next((p for d in _dirs_to_fs_root(start_dir) if (p := find_in(d))), None)


def _config_label(path: str, project_dir: str) -> str:
    label = os.path.relpath(path, os.path.realpath(project_dir))
    if label.startswith(os.pardir):
        label = path
    if os.path.basename(path) == "package.json":
        label += " (eslintConfig)"
    return label


def _eslint_major(install: EslintInstall) -> int | None:
    m = re.match(r"\d+", install.version or "")
    return int(m.group(0)) if m else None


def eslint_config_file(install: EslintInstall, file_dir: str | None = None) -> str | None:
    """The config this ESLint reads for a file in ``file_dir`` (its project
    directory when None), found the way that ESLint version looks, up to the
    filesystem root: ESLint 10 reads only a flat eslint.config.* and looks up
    from the file's directory; ESLint 9 reads only a flat config (a legacy one
    with ESLINT_USE_FLAT_CONFIG=false) and looks up from its cwd, the project
    directory; ESLint 8 prefers a flat config there and falls back to a legacy
    .eslintrc or package.json eslintConfig. An unknown version counts as
    current. Relative to the project directory when inside it; None when
    ESLint would find no config."""
    major = _eslint_major(install)
    current = major is None or major >= 10
    flag = os.environ.get("ESLINT_USE_FLAT_CONFIG")
    if not current and flag == "false":
        found = _nearest(_legacy_eslint_config_in, install.project_dir)
    else:
        start = file_dir if current and file_dir else install.project_dir
        found = _nearest(_flat_eslint_config_in, start)
        if found is None and major is not None and major <= 8 and flag != "true":
            found = _nearest(_legacy_eslint_config_in, install.project_dir)
    return _config_label(found, install.project_dir) if found else None


def eslint_configs_below(install: EslintInstall) -> list[str]:
    """The flat configs in the subdirectories of this ESLint's project
    directory (down to SCAN_DEPTH) that it reads for the files beneath them.
    ESLint 10 looks for its config from each file's directory, so a monorepo
    package's eslint.config.js lints that package even when the project
    directory has none. Empty for ESLint 9 and older, which read only the
    config found from their cwd. An unknown version counts as current."""
    major = _eslint_major(install)
    if major is not None and major < 10:
        return []
    root = os.path.realpath(install.project_dir)
    return [
        config
        for d in workspace_dirs(install.project_dir)
        if d != root and (config := _flat_eslint_config_in(d))
    ]


def missing_eslint_config_reason(install: EslintInstall, file_dir: str | None = None) -> str:
    """Why ESLint has no config to lint with, as a sentence fragment: there is
    none, or only a legacy one this ESLint ignores."""
    legacy = _nearest(_legacy_eslint_config_in, file_dir or install.project_dir)
    if legacy:
        return (
            f"the project has only a legacy {_config_label(legacy, install.project_dir)}, "
            f"which ESLint {install.version} ignores (ESLint 9 and later read "
            "eslint.config.js); migrate it to eslint.config.js"
        )
    return "the project has no ESLint config (eslint.config.js)"


@dataclass
class LanguageServer:
    argv: list[str] | None
    reason: str  # why it cannot start, when argv is None


def language_server(language: str) -> LanguageServer:
    """The command the editor's language-server proxy spawns for ``language``."""
    if language == "python":
        if importlib.util.find_spec("pylsp") is None:
            return LanguageServer(
                None,
                "The Python language server (python-lsp-server) is not installed in the "
                f"app's Python ({sys.executable}).",
            )
        return LanguageServer(python_tool_argv("pylsp"), "")
    if language in ("javascript", "typescript"):
        found = shutil.which("typescript-language-server")
        if not found:
            return LanguageServer(None, TSLS_INSTALL_HINT)
        return LanguageServer([found, "--stdio"], "")
    return LanguageServer(None, f"There is no language server for {language}.")
