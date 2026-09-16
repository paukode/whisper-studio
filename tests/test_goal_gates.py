"""Goal quality gates: storage on goal_state, the runner, and the gate phase
that runs them before the LLM judge."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from server.goals import gates as g
from server.goals import store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    sessions._ensure_db()
    with sessions._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES ('s1', 't', 'a', 'b')"
        )
    yield


def test_gate_store_add_remove_clear_and_counters():
    store.set_goal("s1", "make it green", set_at="2026-01-01T00:00:00Z")
    assert store.get_gates("s1") == []
    store.add_gate("s1", "npm test")
    store.add_gate("s1", "npm test")  # duplicate ignored
    store.add_gate("s1", "ruff check .")
    assert [x["command"] for x in store.get_gates("s1")] == ["npm test", "ruff check ."]
    store.record_gate_results("s1", {"npm test": False, "ruff check .": True})
    store.record_gate_results("s1", {"npm test": False})
    assert store.get_gates("s1")[0]["failures"] == 2
    store.record_gate_results("s1", {"npm test": True})
    assert store.get_gates("s1")[0]["failures"] == 0
    store.remove_gate("s1", 1)
    assert [x["command"] for x in store.get_gates("s1")] == ["ruff check ."]
    store.remove_gate("s1", 9)  # out of range is a no-op
    store.clear_gates("s1")
    assert store.get_gates("s1") == []
    # Gates survive goal state bookkeeping and go with the goal.
    store.add_gate("s1", "make test")
    store.record_block("s1", "not_achieved", "x")
    assert store.get_gates("s1")
    store.clear_goal("s1")
    assert store.get_gates("s1") == []


def test_run_gate_reports_exit_code_and_tail(tmp_path):
    ok = g.run_gate("echo hello", str(tmp_path))
    assert ok.passed and ok.exit_code == 0 and "hello" in ok.tail
    bad = g.run_gate("echo boom >&2; exit 3", str(tmp_path))
    assert not bad.passed and bad.exit_code == 3 and "boom" in bad.tail
    fb = g.format_failure([ok, bad])
    assert fb.startswith("[gate]") and "exit code 3" in fb and "boom" in fb


@pytest.fixture
def _gate_env(monkeypatch):
    from server import hooks
    from server.goals import gate

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "goal_loop")
    return gate


def _ctx(**kw):
    from server.goals import GateContext

    base = {"session_id": "s1", "messages": [{"role": "user", "content": "go"}]}
    base.update(kw)
    return GateContext(**base)


def test_failing_gate_blocks_without_calling_the_evaluator(_gate_env, monkeypatch):
    from server.goals import evaluator

    store.set_goal("s1", "green", set_at="2026-01-01T00:00:00Z")
    store.add_gate("s1", "false-cmd")
    monkeypatch.setattr(
        g, "run_gates", lambda cmds, ws: [g.GateResult(cmds[0], False, 1, "assertion failed")]
    )
    called = []
    monkeypatch.setattr(evaluator, "evaluate", lambda *a, **k: called.append(1))
    d = asyncio.run(_gate_env.run_completion_gate(_ctx()))
    assert d.block is True and d.source == "gate"
    assert "quality gate" in d.feedback and "assertion failed" in d.feedback
    assert called == []
    assert store.get_gates("s1")[0]["failures"] == 1


def test_gate_exhaustion_pauses_the_goal(_gate_env, monkeypatch):
    store.set_goal("s1", "green", set_at="2026-01-01T00:00:00Z")
    store.add_gate("s1", "false-cmd")
    monkeypatch.setattr(g, "run_gates", lambda cmds, ws: [g.GateResult(cmds[0], False, 1, "no")])
    for _ in range(g.GATE_MAX_RETRIES - 1):
        d = asyncio.run(_gate_env.run_completion_gate(_ctx()))
        assert d.block is True
    d = asyncio.run(_gate_env.run_completion_gate(_ctx()))
    assert d.block is False and d.source == "gate"
    assert d.frame["goal_eval"]["verdict"] == "blocked"
    assert store.is_active("s1") is False


def test_passing_gates_fall_through_to_the_evaluator(_gate_env, monkeypatch):
    from server.goals import Verdict, evaluator

    store.set_goal("s1", "green", set_at="2026-01-01T00:00:00Z")
    store.add_gate("s1", "true-cmd")
    monkeypatch.setattr(g, "run_gates", lambda cmds, ws: [g.GateResult(cmds[0], True, 0, "ok")])
    monkeypatch.setattr(evaluator, "evaluate", lambda *a, **k: Verdict("achieved", "all done", 0.9))
    d = asyncio.run(_gate_env.run_completion_gate(_ctx()))
    assert d.block is False and d.goal_achieved is True
