"""The project's formatter after a code write, told to the model only.

A field session re-read every Python file after each write: ruff reformatted
it, the result said "re-read it before editing it again", and the write gate
refused the next edit until the model did. These pin that the formatter's
change goes to the model as a note with the change itself, that the activity
row never shows it, that the next edit needs no re-read, and that JS/TS gets
the project's Prettier and ESLint the same way (server/code_tools/after_write.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import file_state, workspace
from server import tool_executor as te
from server.approval import cards as approval_cards
from server.approval import registry
from server.approval.executors import _do_write
from server.approval.markers import approval_request
from server.approval.router import router as approval_router
from server.approval.spec import ApprovalOutcome, ApprovalSpec
from server.code_tools import after_write, js_format
from server.code_tools.commands import EslintInstall, ToolRun
from server.workspace import executors
from tests.test_ask_user_pause_resume import _State

SID = "format-after-write"
RUFF_PYPROJECT = '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["F"]\n'
# Long enough that the formatter's change is shown as a diff, not the file.
BODY = "".join(f"v{i} = {i}\n" for i in range(40))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    yield tmp_path
    file_state.clear_session(SID)


def _call(executor, **given) -> str:
    return executor({**given, "__session_id__": SID}, [], [])


def _approve(out: str) -> ApprovalOutcome:
    assert out.startswith("[WS_APPROVAL]"), out
    outcome = asyncio.run(_do_write(json.loads(out[len("[WS_APPROVAL]") :])))
    assert outcome.ok, outcome.error
    return outcome


# ── Python: ruff reformats, the model sees the change, no re-read ───────────


def test_a_ruff_reformat_is_shown_to_the_model_and_the_next_edit_needs_no_reread(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    (ws / "a.py").write_text(BODY + "x = 1\n")
    _call(executors._exec_ws_read_file, path="a.py")
    outcome = _approve(
        _call(
            executors._exec_ws_edit_file, path="a.py", old_string="x = 1", new_string="x=abs( 1 )"
        )
    )
    assert (ws / "a.py").read_text() == BODY + "x = abs(1)\n"
    # The row shows the write; the formatter speaks to the model only.
    assert "[ruff]" not in outcome.output and "formatter" not in outcome.output
    assert "formatter changed a.py" in outcome.model_note
    assert "the change:" in outcome.model_note
    assert "-x=abs( 1 )\n+x = abs(1)" in outcome.model_note
    assert "v0 = 0" not in outcome.model_note  # only the changed part
    assert "re-read" not in outcome.model_note
    # The app ran the formatter, so the gate knows the file as it is now.
    nxt = _call(executors._exec_ws_edit_file, path="a.py", old_string="abs(1)", new_string="abs(2)")
    assert nxt.startswith("[WS_APPROVAL]"), nxt


def test_a_small_file_is_shown_whole_when_that_is_shorter_than_the_diff(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    outcome = _approve(_call(executors._exec_ws_create_file, path="s.py", content="y=abs( 2 )\n"))
    assert "the whole file now:\n```\ny = abs(2)\n```" in outcome.model_note


def test_a_write_the_formatter_leaves_alone_has_no_change_note(ws):
    (ws / "pyproject.toml").write_text(RUFF_PYPROJECT)
    outcome = _approve(_call(executors._exec_ws_create_file, path="b.py", content="y = 2\n"))
    assert "formatter changed" not in outcome.model_note
    assert "Remaining: no issues." in outcome.model_note


def test_a_non_code_write_has_no_note(ws):
    outcome = _approve(_call(executors._exec_ws_create_file, path="n.md", content="# hi\n"))
    assert outcome.model_note is None


# ── JS/TS: Prettier, then ESLint --fix, only where the project sets them up ──


@pytest.fixture
def js(ws, monkeypatch):
    """Fake node, Prettier and ESLint installs; run_tool records each run and
    lets a test decide what it does to the file."""
    install = EslintInstall(project_dir=str(ws), entry="/x/tool.js", version="10.0.0")
    monkeypatch.setattr(js_format, "node_path", lambda: "/x/node")
    monkeypatch.setattr(js_format, "find_workspace_prettier", lambda w, d: install)
    monkeypatch.setattr(js_format, "find_workspace_eslint", lambda w, d: install)
    monkeypatch.setattr(
        js_format, "workspace_prettier_config", lambda w, d: str(ws / ".prettierrc")
    )
    monkeypatch.setattr(js_format, "eslint_config_file", lambda i, d: "eslint.config.js")
    runs: list[list[str]] = []
    state = {"prettier": None, "eslint": ToolRun(0, "[]", "")}

    def run_tool(argv, **kw):
        runs.append(argv)
        if "--write" in argv:
            if state["prettier"]:
                with open(argv[-1], "w") as f:
                    f.write(state["prettier"])
            return ToolRun(0, "", "")
        return state["eslint"]

    monkeypatch.setattr(js_format, "run_tool", run_tool)
    return runs, state


def test_js_gets_prettier_then_eslint_fix_and_the_model_sees_the_change(ws, js):
    runs, state = js
    body = "".join(f"export const v{i} = {i};\n" for i in range(40))
    state["prettier"] = body + "const a = 1;\n"
    outcome = _approve(
        _call(executors._exec_ws_create_file, path="src/a.ts", content=body + "const a=1\n")
    )
    assert [("--write" in r, "--fix" in r) for r in runs] == [(True, False), (False, True)]
    assert (ws / "src" / "a.ts").read_text().endswith("const a = 1;\n")
    assert "formatter changed src/a.ts" in outcome.model_note
    assert "-const a=1\n+const a = 1;" in outcome.model_note
    assert "formatter" not in outcome.output
    nxt = _call(
        executors._exec_ws_edit_file,
        path="src/a.ts",
        old_string="const a = 1;",
        new_string="const a = 2;",
    )
    assert nxt.startswith("[WS_APPROVAL]"), nxt


def test_eslint_findings_it_could_not_fix_go_to_the_model(ws, js):
    _, state = js
    found = [{"messages": [{"line": 3, "column": 7, "severity": 2, "message": "x is unused",
                            "ruleId": "no-unused-vars"}]}]  # fmt: skip
    state["eslint"] = ToolRun(1, json.dumps(found), "")
    outcome = _approve(_call(executors._exec_ws_create_file, path="b.js", content="let x\n"))
    assert "[eslint] 1 issue left in b.js" in outcome.model_note
    assert "b.js:3:7: error x is unused (no-unused-vars)" in outcome.model_note


def test_a_file_eslint_ignores_is_not_a_finding(ws, js):
    _, state = js
    ignored = [{"messages": [{"severity": 1, "message": "File ignored because of a matching"}]}]
    state["eslint"] = ToolRun(0, json.dumps(ignored), "")
    outcome = _approve(_call(executors._exec_ws_create_file, path="c.js", content="1\n"))
    assert outcome.model_note is None


def test_without_a_prettier_or_eslint_config_nothing_runs(ws, js, monkeypatch):
    runs, _ = js
    monkeypatch.setattr(js_format, "workspace_prettier_config", lambda w, d: None)
    monkeypatch.setattr(js_format, "eslint_config_file", lambda i, d: None)
    outcome = _approve(_call(executors._exec_ws_create_file, path="d.ts", content="x\n"))
    assert runs == []
    assert outcome.model_note is None


def test_js_outside_the_workspace_is_left_alone(ws, js, tmp_path_factory):
    runs, _ = js
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "e.ts"
    elsewhere.write_text("x\n")
    assert after_write.after_write(str(ws), str(elsewhere)) == ""
    assert runs == []


# ── The note reaches the model and never the row, on both approval paths ────


def _register(action: str, note: str):
    async def fake_exec(payload):
        return ApprovalOutcome(ok=True, output="Wrote a.py (2 bytes).", model_note=note)

    registry.register(
        action,
        ApprovalSpec(category="test-note", preview="diff", summary="w", executor=fake_exec),
    )


def test_an_inline_write_appends_the_note_to_the_tool_result_not_the_row():
    _register("test_note_inline", "[ruff] reformatted")
    payload = json.dumps({"action": "test_note_inline", "path": "a.py", "content": "x\n"})
    state = _State("toolu_n", "ws_write_file", approval_request(payload))
    results, sse_events, pending, _q = asyncio.run(
        te.process_tool_results(
            [state],
            budget_fn=lambda n, o: o,
            session_approvals={"test-note": "allow"},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )
    assert pending is False
    assert results[0]["content"] == "Wrote a.py (2 bytes).\n\n[ruff] reformatted"
    frame = next(
        json.loads(e) for e in sse_events if json.loads(e).get("skill_result") == "ws_write_file"
    )
    assert frame["output"] == "Wrote a.py (2 bytes)."


def test_the_card_post_returns_the_note_for_the_continuation():
    _register("test_note_card", "[eslint] 1 issue left")
    app = FastAPI()
    app.include_router(approval_router)
    with TestClient(app) as client:
        card_id = approval_cards.issue(
            session_id="s", action="test_note_card", payload={"path": "a"}, tool_use_id=None
        )
        res = client.post("/api/approval/execute", json={"card_id": card_id})
    assert res.json()["output"] == "Wrote a.py (2 bytes)."
    assert res.json()["model_note"] == "[eslint] 1 issue left"
