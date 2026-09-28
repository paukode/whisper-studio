"""The Code tools status probe and the editor's language-server proxy.

- GET /api/code-tools/status probes the commands the features really run (the
  app's interpreter for ruff and pylsp, the workspace's ESLint with the app's
  node), off the event loop, and says per tool what it powers, whether it
  works, its version and why not.
- The proxy tells the editor why a language server cannot start or stopped,
  as a JSON-RPC window/showMessage error, instead of closing silently.
"""

import inspect
import json
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server.lsp as lsp
from server import lsp_proxy, workspace
from server.code_tools import commands, ruff, status
from server.code_tools.commands import LanguageServer, ToolRun


def _client(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, base_url="http://localhost")


def _rows(payload) -> dict:
    return {row["id"]: row for row in payload["tools"]}


def _venv_version(module: str) -> str:
    out = subprocess.run(
        [sys.executable, "-P", "-m", module, "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return commands.parse_version(out.stdout)


def test_status_route_runs_in_the_threadpool_not_on_the_event_loop():
    assert not inspect.iscoroutinefunction(status.code_tools_status)


def test_the_old_lsp_status_route_is_gone():
    assert not hasattr(lsp, "router")


def test_status_reports_the_apps_own_ruff_and_pylsp(monkeypatch):
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: "")
    rows = _rows(_client(status.router).get("/api/code-tools/status").json())
    assert rows["ruff"]["ok"] is True
    assert rows["ruff"]["version"] == _venv_version("ruff")
    assert rows["ruff"]["command"].startswith(sys.executable)
    assert rows["pylsp"]["ok"] is True
    assert rows["pylsp"]["version"] == _venv_version("pylsp")
    assert rows["pylsp"]["command"] == f"{sys.executable} -P -m pylsp"
    for row in rows.values():
        assert row["powers"]


def test_status_without_a_workspace_asks_for_one_for_eslint(monkeypatch):
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: "")
    payload = _client(status.router).get("/api/code-tools/status").json()
    assert payload["workspace"] is None
    eslint = _rows(payload)["eslint"]
    assert eslint["ok"] is False
    assert "Connect a workspace" in eslint["reason"]


def test_status_names_a_workspace_without_eslint(tmp_path):
    eslint = _rows(status.status_report(str(tmp_path)))["eslint"]
    assert eslint["ok"] is False
    assert "no ESLint" in eslint["reason"]


def test_status_shows_the_workspaces_eslint_without_running_it(tmp_path, monkeypatch):
    pkg = tmp_path / "node_modules" / "eslint"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "10.2.1", "bin": "bin/eslint.js"}))
    (pkg / "bin" / "eslint.js").write_text("")
    (tmp_path / "eslint.config.js").write_text("export default [];\n")
    calls = []

    def fake_run(argv, *, cwd=None, timeout=10.0):
        calls.append(argv)
        return ToolRun(0, "v22.16.0\n", "")

    monkeypatch.setattr(status, "node_path", lambda: "/app/bin/node")
    monkeypatch.setattr(status, "run_tool", fake_run)
    row = status._eslint(str(tmp_path))
    assert row.ok is True
    # The version is the package's own, and the command is the one
    # lsp_diagnostics runs: the workspace's CLI script with the app's node.
    assert row.version == "10.2.1"
    assert row.command.startswith("/app/bin/node ")
    assert row.command.endswith("node_modules/eslint/bin/eslint.js")
    assert "npx" not in row.command
    assert "eslint.config.js" in row.note
    # Opening the page executes nothing from the workspace: only the app's
    # node is asked for its version.
    assert calls == [["/app/bin/node", "--version"]]
    assert "Node 22.16.0" in row.note


def _fake_eslint(project, version="10.2.1") -> None:
    pkg = project / "node_modules" / "eslint"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": version}))
    (pkg / "bin" / "eslint.js").write_text("")


def _eslint_row(ws, monkeypatch, version="10.2.1"):
    monkeypatch.delenv("ESLINT_USE_FLAT_CONFIG", raising=False)
    monkeypatch.setattr(status, "node_path", lambda: "/app/bin/node")

    def fake_run(argv, **kw):
        return ToolRun(0, f"v{version}\n", "")

    monkeypatch.setattr(status, "run_tool", fake_run)
    return status._eslint(str(ws))


def test_status_does_not_count_a_legacy_config_that_eslint_ignores(tmp_path, monkeypatch):
    # ESLint 9 and 10 exit 2 on a workspace that has only .eslintrc, so the
    # page must not call it working.
    _fake_eslint(tmp_path, "10.2.1")
    (tmp_path / ".eslintrc.json").write_text("{}\n")
    row = _eslint_row(tmp_path, monkeypatch, "10.2.1")
    assert row.ok is False
    assert "only a legacy .eslintrc.json" in row.reason
    assert "migrate it to eslint.config.js" in row.reason


def test_status_counts_a_legacy_config_for_an_eslint_that_reads_it(tmp_path, monkeypatch):
    _fake_eslint(tmp_path, "8.57.0")
    (tmp_path / ".eslintrc.json").write_text("{}\n")
    row = _eslint_row(tmp_path, monkeypatch, "8.57.0")
    assert row.ok is True
    assert "Uses .eslintrc.json" in row.note


def test_status_finds_the_eslint_of_a_monorepo_package(tmp_path, monkeypatch):
    # lsp_diagnostics uses the ESLint nearest the file, so a package's own
    # ESLint works even when the root has none.
    web = tmp_path / "packages" / "web"
    web.mkdir(parents=True)
    _fake_eslint(web)
    (web / "eslint.config.js").write_text("export default [];\n")
    row = _eslint_row(tmp_path, monkeypatch)
    assert row.ok is True
    assert row.command.endswith("packages/web/node_modules/eslint/bin/eslint.js")
    assert "only files under packages/web get ESLint checks" in row.note


def test_status_counts_a_package_config_that_eslint_10_reads_per_file(tmp_path, monkeypatch):
    # ESLint 10 looks for its config from each file's directory, so a root
    # ESLint with the only config in packages/app still lints packages/app.
    _fake_eslint(tmp_path, "10.2.1")
    app = tmp_path / "packages" / "app"
    app.mkdir(parents=True)
    (app / "eslint.config.js").write_text("export default [];\n")
    row = _eslint_row(tmp_path, monkeypatch, "10.2.1")
    install = commands.eslint_install_in(str(tmp_path.resolve()))
    # What lsp_diagnostics finds for a file there is what the row reports.
    assert commands.eslint_config_file(install, str(app)) is not None
    assert row.ok is True
    assert "packages/app/eslint.config.js does" in row.note
    assert "files under packages/app get ESLint checks" in row.note


def test_status_ignores_a_package_config_that_eslint_9_does_not_read(tmp_path, monkeypatch):
    # ESLint 9 reads only the config found from its cwd, the project directory.
    _fake_eslint(tmp_path, "9.30.0")
    app = tmp_path / "packages" / "app"
    app.mkdir(parents=True)
    (app / "eslint.config.js").write_text("export default [];\n")
    row = _eslint_row(tmp_path, monkeypatch, "9.30.0")
    install = commands.eslint_install_in(str(tmp_path.resolve()))
    assert commands.eslint_config_file(install, str(app)) is None
    assert row.ok is False
    assert "no ESLint config" in row.reason


def test_status_probes_ruff_and_pylsp_where_the_features_run_them(tmp_path, monkeypatch):
    cwds = {}

    def fake_run(argv, *, cwd=None, timeout=10.0, stdin=None):
        cwds[argv[3]] = cwd
        return ToolRun(0, "[]" if "check" in argv else "0.15.0\n", "")

    monkeypatch.setattr(status, "run_tool", fake_run)
    status._ruff(str(tmp_path))
    status._pylsp(str(tmp_path))
    assert cwds == {"ruff": str(tmp_path), "pylsp": str(tmp_path)}


def test_status_flags_an_eslint_install_with_no_config(tmp_path, monkeypatch):
    pkg = tmp_path / "node_modules" / "eslint"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "10.2.1"}))
    (pkg / "bin" / "eslint.js").write_text("")
    monkeypatch.setattr(status, "node_path", lambda: "/app/bin/node")
    monkeypatch.setattr(status, "run_tool", lambda argv, **kw: ToolRun(0, "v10.2.1\n", ""))
    row = status._eslint(str(tmp_path))
    assert row.ok is False
    assert "no ESLint config" in row.reason


def test_status_explains_a_missing_typescript_language_server(monkeypatch):
    monkeypatch.setattr(status.shutil, "which", lambda name: None)
    row = status._tsls()
    assert row.ok is False
    assert "npm install -g typescript-language-server" in row.reason


def test_status_says_whether_ruff_fixes_and_formats_in_this_workspace(tmp_path):
    before = _rows(status.status_report(str(tmp_path)))["ruff"]["note"]
    assert "only reports issues" in before
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    after = _rows(status.status_report(str(tmp_path)))["ruff"]["note"]
    assert "ruff.toml" in after
    assert "formats the file" in after


def test_status_and_after_write_agree_on_a_subproject_ruff_config(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n')
    (sub / "m.py").write_text("import os\nx=1\n")
    note = status._ruff(str(tmp_path)).note
    written = ruff.after_write(str(tmp_path), str(sub / "m.py"), "sub/m.py")
    # after_write fixed and formatted the file, so the page must say so.
    assert (sub / "m.py").read_text() == "x = 1\n"
    assert "configures ruff (sub/pyproject.toml)" in written
    assert "sub/pyproject.toml does: after a write under sub" in note
    assert "formats the file" in note


def test_a_ruff_config_ruff_rejects_is_not_working(tmp_path):
    # Every lsp_diagnostics and post-write check fails there, so the page
    # must not say Working.
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nrequired-version = ">=99"\n')
    row = status._ruff(str(tmp_path))
    assert row.ok is False
    assert "Required version" in row.reason
    assert "cannot check files in this workspace" in row.reason
    assert "could not run" in ruff.check_file(str(tmp_path), str(tmp_path / "a.py"), "a.py")


def test_a_broken_ruff_config_in_a_subproject_is_named(tmp_path):
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "ruff.toml").write_text('required-version = ">=99"\n')
    row = status._ruff(str(tmp_path))
    assert row.ok is False
    assert "files under svc" in row.reason


def test_a_working_ruff_config_is_working(tmp_path):
    (tmp_path / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    assert status._ruff(str(tmp_path)).ok is True


# ── The editor proxy says why ─────────────────────────────────────────────────


def _show_message(frame: dict) -> str:
    assert frame["method"] == "window/showMessage"
    assert frame["params"]["type"] == 1  # Error
    # The marker that tells the editor this is the proxy's failure, not a
    # live server's own error popup.
    assert frame["params"]["source"] == "whisper-studio"
    return frame["params"]["message"]


def test_proxy_tells_the_editor_why_no_language_server_starts(monkeypatch):
    monkeypatch.setattr(
        lsp_proxy, "language_server", lambda lang: LanguageServer(None, "pylsp is not installed")
    )
    c = _client(lsp_proxy.router)
    with c.websocket_connect("/ws/lsp/python") as ws:
        assert _show_message(ws.receive_json()) == "pylsp is not installed"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1011


def test_proxy_reports_a_language_server_that_dies(monkeypatch, tmp_path):
    crash = "import sys; sys.stderr.write('pylsp: plugin import failed\\n'); sys.exit(3)"
    monkeypatch.setattr(
        lsp_proxy,
        "language_server",
        lambda lang: LanguageServer([sys.executable, "-c", crash], ""),
    )
    c = _client(lsp_proxy.router)
    with c.websocket_connect(f"/ws/lsp/python?workspace={tmp_path}") as ws:
        message = _show_message(ws.receive_json())
    assert "exit status 3" in message
    assert "plugin import failed" in message


def test_proxy_reports_a_language_server_that_cannot_be_spawned(monkeypatch, tmp_path):
    missing = str(tmp_path / "gone" / "typescript-language-server")
    monkeypatch.setattr(
        lsp_proxy, "language_server", lambda lang: LanguageServer([missing, "--stdio"], "")
    )
    c = _client(lsp_proxy.router)
    with c.websocket_connect("/ws/lsp/typescript") as ws:
        message = _show_message(ws.receive_json())
    assert message.startswith("The typescript language server could not start")
