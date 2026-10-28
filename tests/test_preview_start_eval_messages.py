"""preview_start names what it read, and preview_eval takes ``await``.

A field session asked six times, over two days, for a launch config that sat
in a project folder's own .whisper/launch.json inside the workspace; the bare
"no entry named" line never said which file was read. The same session twice
evaluated ``({x: await fetch(...)})`` and got a SyntaxError.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.preview import manager
from server.preview.router import execute_preview_tool

CONFIGS = [{"name": "web", "runtimeExecutable": "npm", "runtimeArgs": ["run", "dev"], "port": 5173}]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / ".whisper").mkdir()
    (tmp_path / ".whisper" / "launch.json").write_text(json.dumps({"configurations": CONFIGS}))
    app = tmp_path / "app" / ".whisper"
    app.mkdir(parents=True)
    own = [{"name": "app-live", "runtimeExecutable": "make", "runtimeArgs": ["run"], "port": 8781}]
    (app / "launch.json").write_text(json.dumps({"configurations": own}))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def _start(**given) -> str:
    """What the start reports once the card or the mode lets it run."""
    ok, text = asyncio.run(manager.start_preview_session({**given, "session_id": "c1"}))
    assert not ok
    return text


def test_an_unknown_name_says_which_file_was_read_and_its_names(ws):
    out = _start(session_name="nope")
    assert out.startswith("No launch configuration named 'nope' in ")
    assert str(ws / ".whisper" / "launch.json") in out and "Its configurations: web." in out


def test_a_name_in_a_project_folders_own_file_says_to_pass_the_command(ws):
    out = _start(session_name="app-live", cwd=str(ws / "app"))
    assert "app/.whisper/launch.json has 'app-live'" in out
    assert "pass that entry's runtimeExecutable" in out


def test_an_unresolved_name_still_reaches_the_gate(ws):
    # The config may be added before the card is answered
    # (tests/test_preview_launch_trust.py), so the call is not refused early.
    out = asyncio.run(
        execute_preview_tool("preview_start", {"session_name": "nope", "__session_id__": "c1"})
    )
    assert out.startswith("[WS_APPROVAL]")


class _Page:
    def __init__(self):
        self.seen: list[str] = []

    async def evaluate(self, expression):
        self.seen.append(expression)
        if "await" in expression and not expression.startswith("async"):
            raise RuntimeError("Page.evaluate: SyntaxError: Unexpected identifier 'fetch'")
        return {"rows": 3, "ok": True, "none": None}


def test_eval_runs_an_await_expression_in_an_async_function():
    page = _Page()
    result = asyncio.run(manager._evaluate(page, "({rows: await fetch('/api').then(r => 3)})"))
    assert result == {"rows": 3, "ok": True, "none": None}
    assert page.seen[1].startswith("async () => (")
    assert manager._eval_text(result) == '{"rows": 3, "ok": true, "none": null}'


def test_eval_without_await_fails_as_it_did():
    class Broken:
        async def evaluate(self, expression):
            raise RuntimeError("SyntaxError: Unexpected token")

    with pytest.raises(RuntimeError):
        asyncio.run(manager._evaluate(Broken(), "({a: 1"))
