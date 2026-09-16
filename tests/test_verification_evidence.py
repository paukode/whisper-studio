"""server.goals.verification: the passive evidence ledger and the verify-on-stop
gate phase built on it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.goals import verification as v


def _edit(path, tid="e1"):
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": "ws_edit_file", "input": {"path": path}}
        ],
    }


def _result(tid, text):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
    }


def _cmd(command, tid, name="ws_run_command"):
    key = "code" if name == "run_python" else "command"
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": name, "input": {key: command}}],
    }


def test_classify_verify_command():
    assert v.classify_verify_command("cd repo && python -m pytest -q tests/") == "test"
    assert v.classify_verify_command("npm test") == "test"
    assert v.classify_verify_command("venv/bin/ruff check .") == "lint"
    assert v.classify_verify_command("npx tsc --noEmit") == "typecheck"
    assert v.classify_verify_command("npm run build") == "build"
    assert v.classify_verify_command("ls -la") is None
    assert v.classify_verify_command("") is None


def test_is_code_path_excludes_prose_and_data():
    assert v.is_code_path("server/x.py") and v.is_code_path("src/App.tsx")
    assert not v.is_code_path("README.md") and not v.is_code_path("data.csv")
    assert not v.is_code_path("LICENSE") and not v.is_code_path("config.yaml")


def test_result_failed_recognises_the_apps_exit_code_shapes():
    assert v.result_failed("stuff\n(exit code 1: tests failed)")
    assert v.result_failed("exit_code: 2\nboom")
    assert v.result_failed("[Tool Error] nope")
    assert v.result_failed("3 passed, 1 failed")
    assert not v.result_failed("5 passed in 0.3s")
    assert not v.result_failed("exit_code: 0\nall good")


def test_turn_evidence_fresh_pass_after_edit():
    msgs = [
        {"role": "user", "content": "fix it"},
        _edit("server/a.py"),
        _result("e1", "ok"),
        _cmd("python -m pytest -q", "c1"),
        _result("c1", "12 passed"),
    ]
    ev = v.turn_evidence(msgs)
    assert ev.edited_paths == ["server/a.py"]
    assert ev.fresh_pass is True and not ev.fresh_failures


def test_turn_evidence_pass_before_the_last_edit_is_stale():
    msgs = [
        {"role": "user", "content": "fix it"},
        _cmd("python -m pytest -q", "c1"),
        _result("c1", "12 passed"),
        _edit("server/a.py"),
        _result("e1", "ok"),
    ]
    ev = v.turn_evidence(msgs)
    assert ev.runs and ev.runs[0].fresh is False
    assert ev.fresh_pass is False


def test_turn_evidence_failed_run_and_verify_change_token():
    msgs = [
        {"role": "user", "content": "fix"},
        _edit("a.py"),
        _result("e1", "ok"),
        _cmd("npm test", "c1"),
        _result("c1", "Tests: 1 failed, 3 passed\n(exit code 1: failed)"),
    ]
    ev = v.turn_evidence(msgs)
    assert ev.fresh_failures and ev.fresh_failures[0].kind == "test"
    msgs2 = [
        {"role": "user", "content": "fix"},
        _edit("a.py"),
        _result("e1", "ok"),
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "vc", "name": "verify_change", "input": {}}],
        },
        _result("vc", "... VERIFY PASS"),
    ]
    assert v.turn_evidence(msgs2).fresh_pass is True


def test_verify_on_stop_feedback_only_for_unverified_code_edits(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    msgs = [{"role": "user", "content": "go"}, _edit("server/a.py"), _result("e1", "ok")]
    fb = v.verify_on_stop_feedback(msgs, str(tmp_path))
    assert fb and fb.startswith(v.VERIFY_MARKER) and "server/a.py" in fb
    assert "python -m pytest -q" in fb and "ruff check ." in fb
    docs_only = [{"role": "user", "content": "go"}, _edit("README.md"), _result("e1", "ok")]
    assert v.verify_on_stop_feedback(docs_only, str(tmp_path)) is None
    verified = msgs + [_cmd("python -m pytest -q", "c1"), _result("c1", "1 passed")]
    assert v.verify_on_stop_feedback(verified, str(tmp_path)) is None


def test_verify_nudges_are_capped_per_turn():
    msgs = [{"role": "user", "content": "go"}, _edit("a.py"), _result("e1", "ok")]
    nudged = msgs + [
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": f"[completion gate] {v.VERIFY_MARKER} run tests"},
        {"role": "assistant", "content": "done again"},
        {"role": "user", "content": f"[completion gate] {v.VERIFY_MARKER} run tests"},
    ]
    assert v.verify_nudges_used(nudged) == 2
    assert v.verify_on_stop_feedback(nudged, None) is None


@pytest.fixture
def _gate_env(monkeypatch):
    from server import hooks
    from server.goals import gate

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "verify_on_stop")
    return gate


def test_gate_blocks_once_for_unverified_code_edit(_gate_env):
    from server.goals import GateContext

    msgs = [{"role": "user", "content": "go"}, _edit("a.py"), _result("e1", "ok")]
    decision = asyncio.run(
        _gate_env.run_completion_gate(GateContext(session_id="s", messages=msgs, workspace=None))
    )
    assert decision.block is True and decision.source == "verify"
    assert decision.frame and "stop_hook_block" in decision.frame
    assert decision.feedback.startswith(v.VERIFY_MARKER)


def test_gate_allows_prose_edits_and_verified_turns(_gate_env):
    from server.goals import GateContext

    prose = [{"role": "user", "content": "go"}, _edit("notes.md"), _result("e1", "ok")]
    d1 = asyncio.run(_gate_env.run_completion_gate(GateContext(session_id="s", messages=prose)))
    assert d1.block is False
    verified = [
        {"role": "user", "content": "go"},
        _edit("a.py"),
        _result("e1", "ok"),
        _cmd("python -m pytest -q", "c1"),
        _result("c1", "2 passed"),
    ]
    d2 = asyncio.run(_gate_env.run_completion_gate(GateContext(session_id="s", messages=verified)))
    assert d2.block is False
