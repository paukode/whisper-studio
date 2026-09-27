"""Code tools: the linters and language servers the app runs for the assistant.

Contracts pinned here:
- Python diagnostics run ruff with the app's own interpreter, in the
  workspace, and a ruff that cannot run is reported as such, never as a clean
  file or as a finding. The workspace never shadows the tool: a project file
  named ruff.py or logging.py does not run in the app's interpreter.
- JS/TS diagnostics run the workspace's own ESLint with the app's node and the
  json formatter (never npx); a workspace without ESLint, or an ESLint that
  fails to start, is a status, not the file's diagnostics.
"""

import json
import os
import subprocess
import sys

import pytest

from server import lsp, workspace
from server.code_tools import commands, eslint, ruff
from server.code_tools.commands import ToolRun

DIRTY = "import os\nimport sys\n\n\ndef f( x ):\n    return  x+1\n\n\nprint(sys.argv, f(1))\n"
RUFF_PYPROJECT = '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["F"]\n'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(root))
    return root


# ── lsp_diagnostics on Python runs ruff ──────────────────────────────────────


def test_python_diagnostics_are_ruff_findings(ws):
    (ws / "a.py").write_text(DIRTY)
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.py"})
    # A ruff rule code with its location, not pyflakes' bare message.
    assert "a.py:1:8: F401" in out
    # --no-cache: ruff must not litter the user's project.
    assert not (ws / ".ruff_cache").exists()


def test_python_diagnostics_use_the_apps_interpreter_in_the_workspace(ws, monkeypatch):
    seen = {}

    def fake_run(argv, *, cwd=None, timeout=10.0):
        seen.update(argv=argv, cwd=cwd)
        return ToolRun(0, "[]", "")

    monkeypatch.setattr(ruff, "run_tool", fake_run)
    (ws / "a.py").write_text("x = 1\n")
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.py"})
    assert seen["argv"][:4] == [sys.executable, "-P", "-m", "ruff"]
    assert seen["cwd"] == str(ws)
    assert out == "ruff: no issues in a.py."


def test_a_ruff_that_cannot_run_is_reported_not_passed_off_as_clean(ws, monkeypatch):
    # How `python -P -m ruff` fails when ruff is missing: exit 1 (the same code as
    # "findings") with nothing on stdout.
    monkeypatch.setattr(
        ruff,
        "run_tool",
        lambda argv, **kw: ToolRun(1, "", f"{sys.executable}: No module named ruff"),
    )
    (ws / "a.py").write_text("x = 1\n")
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.py"})
    assert "ruff could not run" in out
    assert "No module named ruff" in out
    assert "no issues" not in out


def _plant_shadowing_modules(root, *names) -> list:
    """Workspace modules named like what the tools import; each leaves a
    marker file when it is imported."""
    for name in names:
        (root / f"{name}.py").write_text(
            f"open({str(root / (name + '.ran'))!r}, 'w').write('ran')\n"
        )
    return [root / f"{name}.ran" for name in names]


def test_a_workspace_module_named_like_ruff_never_runs(ws):
    markers = _plant_shadowing_modules(ws, "ruff", "logging", "json")
    (ws / "a.py").write_text(DIRTY)
    checked = ruff.check_file(str(ws), str(ws / "a.py"), "a.py")
    written = ruff.after_write(str(ws), str(ws / "a.py"), "a.py")
    # The real ruff ran both times and found the unused import.
    assert "a.py:1:8: F401" in checked
    assert "a.py:1:8: F401" in written
    assert not [m for m in markers if m.exists()]
    assert not (ws / "__pycache__").exists()


def test_a_workspace_module_never_shadows_the_python_language_server(tmp_path):
    markers = _plant_shadowing_modules(tmp_path, "pylsp", "logging", "json")
    argv = commands.language_server("python").argv
    # How the editor proxy runs it: from the workspace.
    run = subprocess.run([*argv, "--version"], cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert commands.parse_version(run.stdout)
    assert not [m for m in markers if m.exists()]


def test_the_ruff_config_must_be_inside_the_workspace(tmp_path):
    (tmp_path / "pyproject.toml").write_text(RUFF_PYPROJECT)
    inner = tmp_path / "inner"
    inner.mkdir()
    assert commands.workspace_ruff_config(str(inner), str(inner)) is None
    assert commands.workspace_ruff_config(str(tmp_path), str(inner)) == os.path.realpath(
        tmp_path / "pyproject.toml"
    )


# ── lsp_diagnostics on JS/TS runs the workspace's ESLint ─────────────────────


def _fake_eslint(project) -> str:
    pkg = project / "node_modules" / "eslint"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": "eslint", "version": "10.2.1", "bin": {"eslint": "./bin/eslint.js"}})
    )
    (pkg / "bin" / "eslint.js").write_text("// stand-in\n")
    return str(pkg / "bin" / "eslint.js")


def test_js_diagnostics_run_the_workspaces_eslint_with_the_apps_node(ws, monkeypatch):
    entry = _fake_eslint(ws)
    (ws / "eslint.config.js").write_text("export default [];\n")
    (ws / "src").mkdir()
    (ws / "src" / "a.ts").write_text("x = 1\n")
    seen = {}
    report = [
        {
            "filePath": str(ws / "src" / "a.ts"),
            "messages": [
                {
                    "ruleId": "no-undef",
                    "severity": 2,
                    "message": "'x' is not defined.",
                    "line": 1,
                    "column": 1,
                }
            ],
        }
    ]

    def fake_run(argv, *, cwd=None, timeout=10.0):
        seen.update(argv=argv, cwd=cwd)
        return ToolRun(1, json.dumps(report), "")

    monkeypatch.setattr(eslint, "node_path", lambda: "/app/bin/node")
    monkeypatch.setattr(eslint, "run_tool", fake_run)
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "src/a.ts"})
    assert seen["argv"][:2] == ["/app/bin/node", os.path.realpath(entry)]
    assert seen["argv"][2:4] == ["--format", "json"]
    assert "npx" not in " ".join(seen["argv"])
    assert seen["cwd"] == os.path.realpath(ws)
    assert "src/a.ts:1:1: error 'x' is not defined. (no-undef)" in out


def test_a_workspace_without_eslint_is_a_status_not_diagnostics(ws, monkeypatch):
    monkeypatch.setattr(eslint, "run_tool", lambda *a, **k: pytest.fail("must not run anything"))
    (ws / "a.js").write_text("x = 1\n")
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.js"})
    assert "not available" in out
    assert "not a finding about the file" in out


# How ESLint 9 and 10 fail without a flat config: exit 2, the reason in the
# middle of a banner that ends with a help URL.
NO_FLAT_CONFIG_STDERR = (
    "Oops! Something went wrong! :(\n\nESLint: 10.2.1\n\n"
    "ESLint couldn't find an eslint.config.(js|mjs|cjs) file.\n\n"
    "From ESLint v9.0.0, the default configuration file is now eslint.config.js.\n\n"
    "https://eslint.org/docs/latest/use/configure/migration-guide\n\n"
    "If you still have problems after following the migration guide, please stop by\n"
    "https://eslint.org/chat/help to chat with the team.\n"
)


def _failing_eslint(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(eslint, "node_path", lambda: "/app/bin/node")
    monkeypatch.setattr(eslint, "run_tool", lambda *a, **k: ToolRun(2, "", stderr))


def test_an_eslint_that_fails_to_start_is_a_status_not_diagnostics(ws, monkeypatch):
    _fake_eslint(ws)
    (ws / "a.js").write_text("x = 1\n")
    _failing_eslint(monkeypatch, NO_FLAT_CONFIG_STDERR)
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.js"})
    assert "could not lint a.js (exit status 2): the project has no ESLint config" in out
    assert "chat/help" not in out
    assert ".." not in out
    assert "not a finding about the file" in out


def test_eslint_names_a_legacy_config_it_ignores(ws, monkeypatch):
    _fake_eslint(ws)
    (ws / ".eslintrc.json").write_text("{}\n")
    (ws / "a.js").write_text("x = 1\n")
    _failing_eslint(monkeypatch, NO_FLAT_CONFIG_STDERR)
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.js"})
    assert "only a legacy .eslintrc.json, which ESLint 10.2.1 ignores" in out
    assert "migrate it to eslint.config.js" in out


def test_eslint_with_a_config_reports_the_line_that_says_what_failed(ws, monkeypatch):
    _fake_eslint(ws)
    (ws / "eslint.config.js").write_text("import x from 'eslint-plugin-gone';\n")
    (ws / "a.js").write_text("x = 1\n")
    _failing_eslint(
        monkeypatch,
        "Oops! Something went wrong! :(\n\nESLint: 10.2.1\n\n"
        "Error: Cannot find module 'eslint-plugin-gone'\nRequire stack:\n"
        f"- {ws / 'eslint.config.js'}\n",
    )
    out = lsp.execute_lsp_tool("lsp_diagnostics", {"path": "a.js"})
    assert "(exit status 2): Error: Cannot find module 'eslint-plugin-gone'." in out


def test_the_eslint_config_is_the_one_that_eslint_version_reads(tmp_path, monkeypatch):
    monkeypatch.delenv("ESLINT_USE_FLAT_CONFIG", raising=False)
    (tmp_path / ".eslintrc.json").write_text("{}\n")
    sub = tmp_path / "src" / "ui"
    sub.mkdir(parents=True)

    def install(version):
        return commands.EslintInstall(str(tmp_path), "eslint.js", version)

    # ESLint 8 reads the legacy file; 9 and 10 ignore it.
    assert commands.eslint_config_file(install("8.57.0")) == ".eslintrc.json"
    assert commands.eslint_config_file(install("9.30.0")) is None
    assert commands.eslint_config_file(install("10.2.1")) is None
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")
    assert commands.eslint_config_file(install("9.30.0")) == ".eslintrc.json"
    assert commands.eslint_config_file(install("10.2.1")) is None
    monkeypatch.delenv("ESLINT_USE_FLAT_CONFIG")
    # ESLint 10 looks up from the file's directory, ESLint 9 from its cwd.
    (sub / "eslint.config.js").write_text("export default [];\n")
    assert commands.eslint_config_file(install("10.2.1"), str(sub)) == "src/ui/eslint.config.js"
    assert commands.eslint_config_file(install("9.30.0"), str(sub)) is None


def test_the_failure_line_is_the_one_that_states_it():
    assert commands.first_error_line(NO_FLAT_CONFIG_STDERR) == (
        "ESLint couldn't find an eslint.config.(js|mjs|cjs) file"
    )
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/x/plugin.py", line 3, in <module>\n'
        "    raise TypeError(msg)\n"
        "TypeError: bad plugin\n"
    )
    assert commands.first_error_line(traceback) == "TypeError: bad plugin"
    assert commands.first_error_line("ruff failed\n  Cause: x\nUnknown rule: `ZZZ9`") == (
        "Unknown rule: `ZZZ9`"
    )


# ── The editor's language server command ────────────────────────────────────


def test_the_python_language_server_is_the_apps_interpreter_not_a_console_script():
    server = commands.language_server("python")
    # A bare `pylsp` resolves (or not) through PATH, and the bundled console
    # script's shebang names the build machine's interpreter.
    assert server.argv == [sys.executable, "-P", "-m", "pylsp"]


def test_a_missing_typescript_language_server_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    server = commands.language_server("typescript")
    assert server.argv is None
    assert "npm install -g typescript-language-server" in server.reason
