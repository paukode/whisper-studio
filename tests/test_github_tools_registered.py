"""The GitHub tools (github, github_api, github_api_write) run when called.

They were in the catalog, but their executor module was never imported at
boot, so every call answered "Unknown tool". These tests pin that the boot
import list registers them, that a read runs with no card, that a mutation
follows the permission mode at the approval gate, and that a missing gh or a
gh with no sign-in fails with a clear message.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import server.git.gh_common as gh_common
import server.git.gh_executor as gx
from server.approval.bootstrap import register_defaults
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    MODE_PLAN,
    VALID_MODES,
)
from server.tool_executor import process_tool_results

register_defaults()

REPO = Path(__file__).resolve().parent.parent
GITHUB_TOOLS = ("github", "github_api", "github_api_write")
CARD_MODES = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN, MODE_AUTO)


# -- registration ---------------------------------------------------------------


def test_boot_imports_register_every_executor():
    """Import exactly the modules server/main.py imports at boot, in a fresh
    interpreter (another test may already have imported gh_executor here), and
    check every @register_executor name in server/ is dispatchable."""
    script = textwrap.dedent(
        """
        import ast, importlib, json, pathlib
        root = pathlib.Path("server")
        mods = []
        for node in ast.walk(ast.parse((root / "main.py").read_text())):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_EXECUTOR_MODULES" for t in node.targets
            ):
                mods = [e.value for e in node.value.elts]
        for name in mods:
            importlib.import_module(name)
        from server.executors import EXECUTORS
        from server.tasks.tools import BACKGROUND_TASK_TOOL_NAMES
        declared = set()
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
                    == "register_executor"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    declared.add(node.args[0].value)
        # The router dispatches the background-task tools by name before the
        # executor fallback, importing their module itself.
        missing = sorted(declared - set(EXECUTORS) - BACKGROUND_TASK_TOOL_NAMES)
        print(json.dumps({"missing": missing, "has": sorted(set(EXECUTORS))}))
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["missing"] == []
    for name in GITHUB_TOOLS:
        assert name in result["has"]


def test_catalog_lists_the_github_tools():
    from server.chat.tool_pool import assemble_full_catalog

    names = [t["name"] for t in assemble_full_catalog()]
    for name in GITHUB_TOOLS:
        assert names.count(name) == 1


def test_reads_are_read_only_and_writes_are_not():
    from server.executors import EXECUTOR_META

    assert EXECUTOR_META["github_api"]["read_only"] is True
    assert EXECUTOR_META["github"]["read_only"] is False
    assert EXECUTOR_META["github_api_write"]["read_only"] is False


def test_read_only_agents_never_get_the_github_writes():
    from server.agents.config import WRITE_TOOLS

    assert {"github", "github_api_write"} <= WRITE_TOOLS
    assert "github_api" not in WRITE_TOOLS


# -- the read path ---------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr(
        "server.workspace.state.load_workspace_config", lambda: {"path": str(tmp_path)}
    )
    return tmp_path


def test_a_read_dispatches_and_runs_with_no_card(repo, monkeypatch):
    from server.skills import execute_tool

    calls = []

    def fake_run_gh(args, *, cwd, timeout=30, approved=False):
        calls.append((args, approved))
        return 0, '{"login": "octo"}'

    monkeypatch.setattr(gx, "run_gh", fake_run_gh)
    out = execute_tool("github_api", {"endpoint": "user"}, "", None)
    assert out == '{"login": "octo"}'
    assert calls == [(["api", "user"], False)]
    out = execute_tool("github", {"args": ["pr", "list"]}, "", None)
    assert not out.startswith("Unknown tool")
    assert calls[-1][0][:2] == ["pr", "list"]


# -- the approval gate, per permission mode ---------------------------------------


class _State:
    def __init__(self, tool_name, output, root):
        self.tool_id = "tu_gh"
        self.tool_name = tool_name
        self.output = output
        self.side_effects = []
        self.status = "pending"
        self.duration_ms = 0
        self.stats = None
        self.ws_root_taken = True
        self.ws_root = root


def _gate(tool_name, output, root, *, mode, unattended=False):
    results, events, _pending, _q = asyncio.run(
        process_tool_results(
            [_State(tool_name, output, root)],
            budget_fn=lambda _n, out: out,
            session_approvals={},
            config={},
            model_id="",
            recent_messages=[],
            mode=mode,
            unattended=unattended,
        )
    )
    request = None
    for raw in events:
        event = json.loads(raw)
        if "approval_request" in event:
            request = event["approval_request"]
    return json.dumps(results), request


@pytest.fixture
def gate_env(repo, monkeypatch):
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": MODE_DEFAULT},
    )
    sent = []

    def fake_run_gh(args, *, cwd, timeout=30, approved=False):
        if approved:
            sent.append(args)
        return 0, ""

    monkeypatch.setattr(gx, "run_gh", fake_run_gh)
    return repo, sent


def _api_write_call():
    return gx._exec_github_api_write(
        {
            "endpoint": "repos/o/r/issues",
            "method": "POST",
            "body": {"title": "Bug"},
            "__session_id__": "s1",
        },
        None,
        None,
    )


def test_every_mode_is_covered():
    assert set(CARD_MODES) | {MODE_BYPASS, MODE_DONT_ASK} == set(VALID_MODES)


def test_api_write_card_shows_method_path_and_body(gate_env):
    root, sent = gate_env
    for mode in CARD_MODES:
        _out, request = _gate("github_api_write", _api_write_call(), str(root), mode=mode)
        assert request is not None, mode
        assert request["action"] == "github_api_write"
        assert request["summary"] == "GitHub API: POST repos/o/r/issues"
        command = request["payload"]["command"]
        assert command.startswith("gh api --method POST repos/o/r/issues")
        assert '"title": "Bug"' in command
    assert sent == []


def test_api_write_runs_in_bypass(gate_env):
    root, sent = gate_env
    _out, request = _gate("github_api_write", _api_write_call(), str(root), mode=MODE_BYPASS)
    assert request is None
    assert len(sent) == 1 and sent[0][:4] == ["api", "repos/o/r/issues", "--method", "POST"]


def test_api_write_is_denied_in_dont_ask(gate_env):
    root, sent = gate_env
    out, request = _gate("github_api_write", _api_write_call(), str(root), mode=MODE_DONT_ASK)
    assert request is None and "[Denied" in out
    assert sent == []


def test_api_write_is_refused_unattended(gate_env):
    root, sent = gate_env
    for mode in VALID_MODES:
        out, request = _gate(
            "github_api_write", _api_write_call(), str(root), mode=mode, unattended=True
        )
        assert request is None, mode
        assert "not permitted from an unattended subagent" in out, mode
    assert sent == []


def test_gh_verb_write_follows_the_mode(gate_env):
    root, sent = gate_env

    def call():
        return gx._exec_github({"args": ["issue", "create", "--title", "x"]}, None, None)

    for mode in CARD_MODES:
        _out, request = _gate("github", call(), str(root), mode=mode)
        assert request is not None and request["payload"]["command"].startswith("gh issue create")
    out, request = _gate("github", call(), str(root), mode=MODE_DONT_ASK)
    assert request is None and "[Denied" in out
    assert sent == []
    _out, request = _gate("github", call(), str(root), mode=MODE_BYPASS)
    assert request is None and sent == [["issue", "create", "--title", "x"]]


def test_destructive_github_still_always_asks_in_bypass(gate_env):
    root, sent = gate_env
    out = gx._exec_github({"args": ["pr", "merge", "2"]}, None, None)
    _out, request = _gate("github", out, str(root), mode=MODE_BYPASS)
    assert request is not None and request["always_asks"] is True
    assert sent == []


def test_destructive_github_is_denied_in_dont_ask(gate_env):
    """Don't ask never shows a card, so a call that needs a person is denied."""
    root, sent = gate_env
    merge = gx._exec_github({"args": ["pr", "merge", "2"]}, None, None)
    out, request = _gate("github", merge, str(root), mode=MODE_DONT_ASK)
    assert request is None and "[Denied" in out
    delete = gx._exec_github_api_write(
        {"endpoint": "repos/o/r/git/refs/heads/x", "method": "DELETE"}, None, None
    )
    out, request = _gate("github_api_write", delete, str(root), mode=MODE_DONT_ASK)
    assert request is None and "[Denied" in out
    # The same DELETE is a destructive call: in Bypass it still shows the card.
    _out, request = _gate("github_api_write", delete, str(root), mode=MODE_BYPASS)
    assert request is not None and request["always_asks"] is True
    assert sent == []


def test_destructive_github_asks_in_every_card_mode(gate_env):
    root, sent = gate_env
    for mode in CARD_MODES:
        merge = gx._exec_github({"args": ["pr", "merge", "2"]}, None, None)
        _out, request = _gate("github", merge, str(root), mode=mode)
        assert request is not None and request["always_asks"] is True, mode
    assert sent == []


def test_destructive_github_is_denied_in_dont_ask_and_asks_otherwise(gate_env):
    """Don't ask is stricter than the floor's card, so it denies; every other
    mode, Bypass included, keeps the card."""
    from server.security.permissions import resolve_static_decision

    payload = {"command": "gh pr merge 2"}
    for mode, want in ((MODE_DONT_ASK, "deny"), (MODE_BYPASS, "ask"), (MODE_DEFAULT, "ask")):
        got = resolve_static_decision("github", payload, "github-destructive", {}, mode)
        assert got == want, mode


# -- missing gh and missing sign-in ------------------------------------------------


def test_missing_gh_says_how_to_install(repo, monkeypatch):
    def no_gh(*_a, **_kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(gh_common.subprocess, "run", no_gh)
    out = gx._exec_github_api({"endpoint": "user"}, None, None)
    assert out.startswith("Error: 'gh' CLI not found")
    assert "gh auth login" in out


def test_gh_with_no_sign_in_says_so(repo, monkeypatch):
    def signed_out(argv, **_kw):
        return subprocess.CompletedProcess(
            argv, 4, stdout="", stderr="To get started with GitHub CLI, please run:  gh auth login"
        )

    monkeypatch.setattr(gh_common.subprocess, "run", signed_out)
    out = gx._exec_github_api({"endpoint": "user"}, None, None)
    assert out.startswith("Error: The GitHub CLI is not signed in.")
    out = gx._exec_github({"args": ["pr", "list"]}, None, None)
    assert out.startswith("Error: The GitHub CLI is not signed in.")


def test_no_workspace_refuses_before_gh(monkeypatch):
    monkeypatch.setattr(gx, "get_workspace_path", lambda: None)
    monkeypatch.setattr(gx, "run_gh", lambda *a, **k: pytest.fail("gh ran"))
    out = gx._exec_github_api({"endpoint": "user"}, None, None)
    assert out == "Error: No workspace connected. Connect a workspace first."
