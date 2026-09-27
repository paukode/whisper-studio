"""ruff after every assistant write of a Python file.

Every successful write, create or edit of a Python file (in the workspace
through the approval executor _do_write, to an absolute path through
_do_save_to_path, on every approval path) is checked by ruff, and the findings
ride in the tool result. Only a workspace that
configures ruff (pyproject.toml [tool.ruff], ruff.toml or .ruff.toml) gets the
file fixed and formatted, and the result states what changed. A ruff that
cannot run is said so; the write itself stands.
"""

import asyncio
import threading

import pytest

from server import workspace
from server.approval.bootstrap import _do_write
from server.code_tools import ruff
from server.code_tools.commands import ToolRun

DIRTY = "import os\nimport sys\n\n\ndef f( x ):\n    return  x+1\n\n\nprint(sys.argv, f(1))\n"
RUFF_PYPROJECT = '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["F"]\n'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(root))
    return root


def _write(ws, rel: str, content: str) -> str:
    outcome = asyncio.run(_do_write({"path": rel, "content": content}))
    assert outcome.ok, outcome.error
    return outcome.output


def test_write_in_a_workspace_without_ruff_config_checks_and_changes_nothing(ws):
    out = _write(ws, "a.py", DIRTY)
    assert (ws / "a.py").read_text() == DIRTY
    assert "a.py:1:8: F401" in out
    assert "nothing was fixed or formatted" in out


def test_pyproject_without_a_ruff_table_does_not_opt_in_to_fixes(ws):
    (ws / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    _write(ws, "a.py", DIRTY)
    assert (ws / "a.py").read_text() == DIRTY


def test_write_in_a_ruff_configured_workspace_fixes_formats_and_says_so(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    out = _write(ws, "pkg/a.py", DIRTY)
    on_disk = (ws / "pkg" / "a.py").read_text()
    assert "import os" not in on_disk  # check --fix removed the unused import
    assert "def f(x):\n    return x + 1" in on_disk  # ruff format ran
    assert "pyproject.toml" in out
    assert "fixed 1 issue (F401)" in out
    assert "reformatted" in out
    assert "re-read it before editing it again" in out
    assert "Remaining: no issues." in out
    assert not (ws / ".ruff_cache").exists()


def test_a_file_the_project_excludes_is_left_alone(ws):
    (ws / "pyproject.toml").write_text(
        '[tool.ruff]\nextend-exclude = ["gen"]\n\n[tool.ruff.lint]\nselect = ["F"]\n'
    )
    out = _write(ws, "gen/g.py", DIRTY)
    assert (ws / "gen" / "g.py").read_text() == DIRTY
    assert "excluded by the project's ruff configuration" in out


def test_a_non_python_write_runs_no_ruff(ws, monkeypatch):
    called = []
    monkeypatch.setattr(ruff, "after_write", lambda *a: called.append(a) or "")
    _write(ws, "notes.md", "# hi\n")
    assert called == []


def test_the_post_write_check_runs_off_the_event_loop(ws, monkeypatch):
    threads = {}

    def fake_after_write(ws_path, full, rel):
        threads["check"] = threading.get_ident()
        return "[ruff] fake"

    async def run():
        threads["loop"] = threading.get_ident()
        return await _do_write({"path": "a.py", "content": "x = 1\n"})

    monkeypatch.setattr(ruff, "after_write", fake_after_write)
    outcome = asyncio.run(run())
    assert outcome.output.endswith("[ruff] fake")
    assert threads["check"] != threads["loop"]


def test_ruff_failing_after_a_write_is_said_and_the_write_stands(ws, monkeypatch):
    monkeypatch.setattr(ruff, "run_tool", lambda argv, **kw: ToolRun(2, "", "error: bad config"))
    out = _write(ws, "a.py", "x = 1\n")
    assert (ws / "a.py").read_text() == "x = 1\n"
    assert "Could not check a.py" in out
    assert "error: bad config" in out
    assert "The write itself succeeded" in out


# ── Python saved to an absolute path (save_file, ws_create_file destinations) ──


def _save(tmp_path, monkeypatch, tool: str, args: dict):
    """Run a file-saving tool and approve its card, as the gate does."""
    import json

    import server.workspace.paths as wpaths
    from server.approval import registry
    from server.approval.bootstrap import register_defaults
    from server.workspace import executors as wexec

    if registry.get("save_to_path") is None:
        register_defaults()
    monkeypatch.setattr(wpaths, "DATA_DIR", str(tmp_path / "data"))
    run = {"save_file": wexec._exec_save_file, "ws_create_file": wexec._exec_ws_create_file}
    raw = run[tool](args, "", [])
    card = json.loads(raw[len("[WS_APPROVAL]") :])
    assert card["action"] == "save_to_path", card
    spec = registry.get(card["action"])
    outcome = asyncio.run(spec.executor(spec.build_payload(card)))
    assert outcome.ok, outcome.error
    return outcome.output


def test_a_python_file_saved_into_the_workspace_is_fixed_like_any_write(ws, tmp_path, monkeypatch):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    dest = ws / "b.py"
    out = _save(
        tmp_path,
        monkeypatch,
        "ws_create_file",
        {"path": "b.py", "destination_path": str(dest), "content": DIRTY},
    )
    assert "import os" not in dest.read_text()
    assert "fixed 1 issue (F401)" in out and "b.py" in out


def test_save_file_of_python_into_the_workspace_is_checked(ws, tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    out = _save(
        tmp_path,
        monkeypatch,
        "save_file",
        {"filename": "c.py", "destination_path": str(ws / "c.py"), "content": DIRTY},
    )
    assert "c.py:1:8: F401" in out
    assert (ws / "c.py").read_text() == DIRTY  # no ruff config: checked only


def test_python_saved_outside_the_workspace_is_only_checked(ws, tmp_path, monkeypatch):
    # Even beside a ruff config: outside the workspace nothing opts in to fixes.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    out = _save(
        tmp_path,
        monkeypatch,
        "save_file",
        {"filename": "d.py", "destination_path": str(elsewhere / "d.py"), "content": DIRTY},
    )
    assert (elsewhere / "d.py").read_text() == DIRTY
    assert "d.py:1:8: F401" in out
    assert "saved outside the connected workspace" in out


# ── a config that sets `fix = true` never turns a check into a fix ───────────

FIX_TRUE = '[tool.ruff]\nfix = true\n\n[tool.ruff.lint]\nselect = ["F"]\n'


def test_diagnostics_never_fix_even_when_the_workspace_config_sets_fix(ws):
    (ws / "pyproject.toml").write_text(FIX_TRUE)
    (ws / "a.py").write_text(DIRTY)
    out = ruff.check_file(str(ws), str(ws / "a.py"), "a.py")
    # lsp_diagnostics is read-only (and allowed in plan mode): the file stays
    # byte for byte, and the finding is reported rather than silently fixed.
    assert (ws / "a.py").read_text() == DIRTY
    assert "a.py:1:8: F401" in out


def test_a_fix_true_config_above_the_workspace_changes_nothing(ws):
    (ws.parent / "pyproject.toml").write_text(FIX_TRUE)
    (ws / "b.py").write_text(DIRTY)
    checked = ruff.check_file(str(ws), str(ws / "b.py"), "b.py")
    out = _write(ws, "a.py", DIRTY)
    assert (ws / "b.py").read_text() == DIRTY
    assert "b.py:1:8: F401" in checked
    assert (ws / "a.py").read_text() == DIRTY
    assert "nothing was fixed or formatted" in out
    assert "a.py:1:8: F401" in out


def test_a_configured_fix_is_counted_when_the_config_sets_fix(ws):
    (ws / "pyproject.toml").write_text(FIX_TRUE)
    out = _write(ws, "a.py", DIRTY)
    assert "import os" not in (ws / "a.py").read_text()
    # The first check only reads, so the summary sees what --fix removed.
    assert "fixed 1 issue (F401)" in out
