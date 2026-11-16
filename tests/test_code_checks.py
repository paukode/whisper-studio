"""The Code tools switch (the ``code_checks`` flag) and the checks a chat turn
can end with.

Off, which is the default, the app runs no linter or formatter on the
assistant's code: not after a write and not when a turn ends. On, a turn that
changed code files ends with a ``code_checks_offer`` frame naming them, and
``POST /api/code-tools/check`` runs ruff and ESLint outside the model. Only
findings produce a report for the model; a clean run produces none.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.code_tools import after_write, eslint, ruff, turn_checks
from server.infrastructure.feature_flags import get_flag_states


def _turn(*calls):
    """A turn's history: the prompt, then one assistant row with the calls."""
    return [
        {"role": "user", "content": "change the code"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t{i}", "name": name, "input": given}
                for i, (name, given) in enumerate(calls)
            ],
        },
    ]


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setattr(turn_checks, "enabled", lambda: True)


def test_the_switch_is_off_by_default():
    state = get_flag_states()[turn_checks.FLAG]
    assert state["default"] is False


def test_off_nothing_runs_after_a_write(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_checks, "enabled", lambda: False)

    def ran(*a, **k):
        raise AssertionError("a tool ran with the switch off")

    monkeypatch.setattr(ruff, "after_save", ran)
    f = tmp_path / "a.py"
    f.write_text("import os\n")
    assert after_write.after_write(str(tmp_path), str(f)) == ""


def test_off_a_turn_that_changed_code_gets_no_offer(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_checks, "enabled", lambda: False)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert turn_checks.offer(_turn(("ws_write_file", {"path": "a.py"})), str(tmp_path)) is None


def test_the_offer_names_the_code_files_the_turn_wrote(switched_on, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.ts").write_text("export const x = 1;\n")
    (tmp_path / "notes.md").write_text("notes\n")
    outside = tmp_path.parent / "elsewhere.py"
    outside.write_text("y = 2\n")
    turn = _turn(
        ("ws_write_file", {"path": "a.py"}),
        ("ws_edit_file", {"path": "a.py"}),
        ("ws_create_file", {"path": "b.ts"}),
        ("ws_write_file", {"path": "notes.md"}),
        ("ws_write_file", {"path": "gone.py"}),
        ("save_file", {"destination": str(outside)}),
        ("ws_read_file", {"path": "unread.py"}),
    )
    frame = turn_checks.offer(turn, str(tmp_path))
    written = [str((tmp_path / "a.py").resolve()), str((tmp_path / "b.ts").resolve())]
    assert frame == {"code_checks_offer": {"files": [*written, str(outside.resolve())]}}


def test_a_turn_with_no_code_change_gets_no_offer(switched_on, tmp_path):
    (tmp_path / "notes.md").write_text("notes\n")
    assert turn_checks.offer(_turn(("ws_write_file", {"path": "notes.md"})), str(tmp_path)) is None
    # An earlier turn's write is not this turn's.
    (tmp_path / "a.py").write_text("x = 1\n")
    earlier = [*_turn(("ws_write_file", {"path": "a.py"})), {"role": "user", "content": "thanks"}]
    assert turn_checks.offer(earlier, str(tmp_path)) is None


def test_only_findings_make_a_report_for_the_model(monkeypatch, tmp_path):
    py, ts, skipped = tmp_path / "a.py", tmp_path / "b.ts", tmp_path / "c.py"
    for f in (py, ts, skipped):
        f.write_text("x\n")
    answers = {
        "a.py": (2, "ruff: 2 issues in a.py:\n  a.py:1:1: F401 unused"),
        "c.py": (None, "ruff: c.py is excluded"),
    }
    monkeypatch.setattr(ruff, "check", lambda ws, full, rel: answers[rel])
    monkeypatch.setattr(eslint, "check", lambda ws, full, rel: (0, f"ESLint: no issues in {rel}."))

    out = turn_checks.run(
        str(tmp_path), [str(py.resolve()), str(ts.resolve()), str(skipped.resolve())]
    )
    assert out["issues"] == 2
    assert [r["issues"] for r in out["results"]] == [2, 0, None]
    # The report carries the findings and nothing about the clean or the
    # unchecked file.
    assert "a.py:1:1: F401" in out["report"]
    assert "b.ts" not in out["report"] and "c.py" not in out["report"]

    monkeypatch.setattr(ruff, "check", lambda ws, full, rel: (0, f"ruff: no issues in {rel}."))
    clean = turn_checks.run(str(tmp_path), [str(py.resolve())])
    assert clean["issues"] == 0 and clean["report"] == ""


def test_javascript_outside_the_workspace_is_not_linted(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    js = tmp_path / "loose.js"
    js.write_text("x\n")

    def ran(*a, **k):
        raise AssertionError("ESLint ran on a file outside the workspace")

    monkeypatch.setattr(eslint, "check", ran)
    (result,) = turn_checks.run(str(ws), [str(js.resolve())])["results"]
    assert result["issues"] is None and "outside the connected workspace" in result["text"]


def _client():
    app = FastAPI()
    app.include_router(turn_checks.router)
    return TestClient(app)


def test_the_route_runs_nothing_while_the_switch_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(turn_checks, "enabled", lambda: False)
    monkeypatch.setattr(
        turn_checks, "run", lambda *a: pytest.fail("checks ran with the switch off")
    )
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    body = _client().post("/api/code-tools/check", json={"files": [str(f)]}).json()
    assert body == {"results": [], "issues": 0, "report": "", "enabled": False}


def test_the_route_checks_only_existing_code_files_named_by_absolute_path(
    switched_on, monkeypatch, tmp_path
):
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    seen = []
    monkeypatch.setattr(
        turn_checks,
        "run",
        lambda ws, files: seen.append((ws, files)) or {"results": [], "issues": 0, "report": ""},
    )
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("n\n")
    named = [str(f), str(f), "a.py", str(tmp_path / "notes.md"), str(tmp_path / "gone.py"), 7]
    body = _client().post("/api/code-tools/check", json={"files": named}).json()
    assert seen == [(str(tmp_path), [str(f.resolve())])]
    assert body["enabled"] is True


def _run_chat_turn(monkeypatch, tmp_path, *, completion_gate):
    """One turn through the engine: a round that writes a.py, then the reply."""
    import asyncio

    from server import tool_executor
    from server.chat.engine import runner
    from server.chat.engine.events import RoundResult, Usage
    from server.chat.engine.policy import TurnPolicy
    from server.goals import GateDecision

    (tmp_path / "a.py").write_text("x = 1\n")
    write = {"type": "tool_use", "name": "ws_write_file", "id": "w1", "input": {"path": "a.py"}}
    rounds = [
        RoundResult(stop_reason="tool_use", content=[write], usage=Usage()),
        RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": "Done."}], usage=Usage()
        ),
    ]

    class Adapter:
        provider = "test"

        async def stream_round(self, *args):
            yield rounds.pop(0)

    async def wrote(name, tool_input, **kwargs):
        return "Wrote a.py", []

    async def gate_passes(ctx):
        yield GateDecision()

    monkeypatch.setattr(tool_executor, "route_tool", wrote)
    monkeypatch.setattr("server.goals.gate.run_gate_with_progress", gate_passes)

    async def run():
        ctx = runner.TurnContext(
            session_id="code-checks-turn",
            model_key="gpt6-luna",
            model_id="openai.gpt-6-luna",
            messages=[{"role": "user", "content": "change a.py"}],
            adapter=Adapter(),
            policy=TurnPolicy(max_rounds=4, completion_gate=completion_gate),
            loop=asyncio.get_running_loop(),
            executor=None,
            cost_source="chat",
            memory_hooks=lambda messages: None,
            tool_exec_model_id="",
            ws_path=str(tmp_path),
            mode="bypassPermissions",
            tool_catalog=lambda: (
                [{"name": "ws_write_file", "input_schema": {"type": "object"}}],
                None,
            ),
        )
        return [frame async for frame in runner.run_turn(ctx)]

    return asyncio.run(run())


def test_a_chat_turn_that_wrote_code_ends_with_the_offer(switched_on, monkeypatch, tmp_path):
    frames = _run_chat_turn(monkeypatch, tmp_path, completion_gate=True)
    offers = [f for f in frames if "code_checks_offer" in f]
    assert len(offers) == 1
    assert str((tmp_path / "a.py").resolve()) in offers[0]
    assert frames.index(offers[0]) < frames.index("data: [DONE]\n\n")


def test_a_run_nobody_watches_gets_no_offer(switched_on, monkeypatch, tmp_path):
    # Agents, scheduled runs and headless runs build their policy with the
    # completion gate off: there is nobody to answer the card.
    frames = _run_chat_turn(monkeypatch, tmp_path, completion_gate=False)
    assert not [f for f in frames if "code_checks_offer" in f]
