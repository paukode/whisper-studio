"""WS-E goal loop: store persistence, transcript tail, evaluator coercion, and
the completion gate ordering/caps. Each test isolates WHISPER_DATA_DIR so the
sessions DB is throwaway; the goal columns migrate on first _ensure_db.
"""

from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    # The sessions DB path is a module global derived from __file__, not
    # data_root(), so point it at a per-test file for real isolation.
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    yield


def _mk_session(session_id="s1"):
    from server.infrastructure import sessions

    sessions._ensure_db()
    with sessions._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, "t", "2026-01-01", "2026-01-01"),
        )
    return session_id


def _run(coro):
    return asyncio.run(coro)


# ── store ────────────────────────────────────────────────────────────────────


def test_goal_columns_migrate_on_old_db(tmp_path, monkeypatch):
    # A sessions table created WITHOUT the goal columns must gain them on
    # _ensure_db replay (additive-migration contract).
    from server.infrastructure import sessions

    with sessions._get_conn() as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, custom_title INT DEFAULT 0, "
            "generated_title INT DEFAULT 0, created_at TEXT, updated_at TEXT, "
            "segments TEXT DEFAULT '[]', chat_history TEXT DEFAULT '[]', speaker_names TEXT DEFAULT '{}')"
        )
    sessions._ensure_db()
    with sessions._get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "goal" in cols and "goal_state" in cols


def test_store_set_get_clear():
    from server.goals import store

    sid = _mk_session()
    store.set_goal(sid, "make tests pass", set_at="2026-01-01T00:00:00Z")
    g = store.get_goal(sid)
    assert g["goal"] == "make tests pass"
    assert g["state"]["active"] is True
    assert store.is_active(sid) is True
    store.clear_goal(sid)
    assert store.get_goal(sid)["goal"] == ""
    assert store.is_active(sid) is False


def test_store_block_increment_and_reset():
    from server.goals import store

    sid = _mk_session()
    store.set_goal(sid, "goal")
    assert store.record_block(sid, "not_achieved", "keep going") == 1
    assert store.record_block(sid, "not_achieved", "still") == 2
    assert store.get_goal(sid)["state"]["total_evals"] == 2
    store.reset_for_new_turn(sid)
    assert store.get_goal(sid)["state"]["consecutive_blocks"] == 0


def test_store_achieved_deactivates():
    from server.goals import store

    sid = _mk_session()
    store.set_goal(sid, "goal")
    store.record_pass(sid, "achieved", "done")
    assert store.is_active(sid) is False


# ── tail ─────────────────────────────────────────────────────────────────────


def test_tail_slices_giant_tool_result():
    from server.goals.tail import render_tail

    big = "x" * 50_000
    msgs = [{"role": "user", "content": [{"type": "tool_result", "content": big}]}]
    out = render_tail(msgs)
    assert len(out) < 5_000
    assert "elided" in out


def test_tail_renders_responses_items():
    from server.goals.tail import render_tail

    msgs = [
        {"role": "user", "content": [{"type": "input_text", "text": "do it"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "ok"},
                {"type": "function_call", "name": "ws_bash"},
            ],
        },
    ]
    out = render_tail(msgs)
    assert "do it" in out and "ok" in out and "ws_bash" in out


def test_tail_renders_top_level_responses_tool_items():
    # The OpenAI loop's input_items carry tool activity as TOP-LEVEL items with
    # no role/content — they must still be visible to the evaluator.
    from server.goals.tail import render_tail

    msgs = [
        {"role": "user", "content": [{"type": "input_text", "text": "run the tests"}]},
        {"type": "function_call", "call_id": "c1", "name": "ws_run_command", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "42 passed"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "all green"}]},
    ]
    out = render_tail(msgs)
    assert "ws_run_command" in out
    assert "42 passed" in out
    assert "all green" in out


# ── evaluator ────────────────────────────────────────────────────────────────


def test_evaluator_parses_clean_json(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(
        evaluator,
        "_one_shot",
        lambda s, u: '{"verdict":"achieved","feedback":"ok","confidence":0.9}',
    )
    v = evaluator.evaluate("goal", [{"role": "assistant", "content": "done"}])
    assert v.is_achieved and v.confidence == 0.9


def test_evaluator_extracts_json_from_prose(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(
        evaluator,
        "_one_shot",
        lambda s,
        u: 'Here is my verdict:\n{"verdict": "not_achieved", "feedback": "add tests", "confidence": 0.6}\nDone.',
    )
    v = evaluator.evaluate("goal", [])
    assert v.verdict == "not_achieved" and v.feedback == "add tests"


def test_evaluator_coerces_out_of_enum(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(evaluator, "_one_shot", lambda s, u: '{"verdict":"done","confidence":2.0}')
    v = evaluator.evaluate("goal", [])
    assert v.verdict == "achieved" and v.confidence == 1.0


def test_evaluator_retries_then_allows(monkeypatch):
    from server.goals import evaluator

    calls = {"n": 0}

    def flaky(s, u):
        calls["n"] += 1
        return "not json at all"

    monkeypatch.setattr(evaluator, "_one_shot", flaky)
    v = evaluator.evaluate("goal", [])
    assert calls["n"] == 2  # one retry
    assert v.is_achieved  # allow-with-warning, never wedge


def test_evaluator_no_model_allows(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(evaluator, "_one_shot", lambda s, u: None)
    v = evaluator.evaluate("goal", [])
    assert v.is_achieved and v.confidence == 0.0


def test_looks_verified():
    from server.goals.evaluator import looks_verified

    assert looks_verified([{"role": "assistant", "content": "ran gate\nVERIFY PASS"}])
    assert not looks_verified([{"role": "assistant", "content": "VERIFY FAIL: tests red"}])


# ── gate ─────────────────────────────────────────────────────────────────────


def _ctx(sid, **kw):
    from server.goals import GateContext

    return GateContext(session_id=sid, **kw)


def test_gate_flag_off_short_circuits(monkeypatch):
    from server.goals import gate

    monkeypatch.setattr(gate, "_flag_on", lambda n, d=True: False)
    d = _run(gate.run_completion_gate(_ctx("s1", goal="g")))
    assert not d.block


def test_gate_flag_off_still_runs_stop_hooks(monkeypatch):
    # goal_loop off must disable ONLY the evaluator — Stop hooks are WS-I's
    # contract and the gate replaced the loops' direct check_stop_hooks calls.
    from server.goals import gate
    from server.hooks.schema import HookOutcome

    monkeypatch.setattr(gate, "_flag_on", lambda n, d=True: False)

    async def fake_stop(*a, **k):
        return HookOutcome(decision="deny", reason="stop hook says no")

    monkeypatch.setattr("server.hooks.check_stop_hooks", fake_stop)
    d = _run(gate.run_completion_gate(_ctx("s1", goal="g")))
    assert d.block and d.source == "stop_hook"


def test_gate_calls_real_evaluate_signature(monkeypatch):
    # Regression for the keyword-only `provider` miscall: exercise the REAL
    # evaluate() through the gate by stubbing one level below it (_one_shot),
    # so a signature mismatch raises instead of being swallowed by a lambda.
    from server.goals import evaluator, gate, store

    monkeypatch.setattr(
        evaluator,
        "_one_shot",
        lambda s, u: '{"verdict":"not_achieved","feedback":"keep going","confidence":0.5}',
    )
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(
        gate.run_completion_gate(
            _ctx(
                sid, goal="g", provider="openai", messages=[{"role": "assistant", "content": "wip"}]
            )
        )
    )
    assert d.block and d.source == "evaluator" and "keep going" in d.feedback


def test_gate_fails_open_on_evaluator_crash(monkeypatch):
    from server.goals import gate, store

    def boom(*a, **k):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr("server.goals.evaluator.evaluate", boom)
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(gate.run_completion_gate(_ctx(sid, goal="g")))
    assert not d.block  # fail open: a broken evaluator never aborts the turn


def test_gate_no_goal_no_hooks_is_fast_allow():
    from server.goals import gate

    sid = _mk_session()
    d = _run(gate.run_completion_gate(_ctx(sid)))
    assert not d.block and d.source == ""


def test_gate_stop_hook_blocks_before_evaluator(monkeypatch):
    from server.goals import gate
    from server.hooks.schema import HookOutcome

    async def fake_stop(*a, **k):
        return HookOutcome(decision="deny", reason="goal not met: tests failing")

    monkeypatch.setattr("server.hooks.check_stop_hooks", fake_stop)
    # Evaluator must NOT be called when a stop hook already blocked.
    monkeypatch.setattr(
        "server.goals.evaluator.evaluate", lambda *a, **k: pytest.fail("evaluator ran")
    )
    sid = _mk_session()
    from server.goals import store

    store.set_goal(sid, "g")
    d = _run(gate.run_completion_gate(_ctx(sid, goal="g")))
    assert d.block and d.source == "stop_hook" and "tests failing" in d.feedback


def test_gate_evaluator_blocks_not_achieved(monkeypatch):
    from server.goals import Verdict, gate, store

    monkeypatch.setattr(
        "server.goals.evaluator.evaluate", lambda *a, **k: Verdict("not_achieved", "add tests", 0.5)
    )
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(
        gate.run_completion_gate(
            _ctx(sid, goal="g", messages=[{"role": "assistant", "content": "wip"}])
        )
    )
    assert d.block and d.source == "evaluator" and "add tests" in d.feedback
    assert store.get_goal(sid)["state"]["consecutive_blocks"] == 1


def test_gate_evaluator_achieved_allows_and_deactivates(monkeypatch):
    from server.goals import Verdict, gate, store

    monkeypatch.setattr(
        "server.goals.evaluator.evaluate", lambda *a, **k: Verdict("achieved", "done", 0.95)
    )
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(gate.run_completion_gate(_ctx(sid, goal="g")))
    assert not d.block and d.goal_achieved
    assert store.is_active(sid) is False


def test_gate_confident_blocked_ends_turn(monkeypatch):
    from server.goals import Verdict, gate, store

    monkeypatch.setattr(
        "server.goals.evaluator.evaluate",
        lambda *a, **k: Verdict("blocked", "needs prod creds", 0.9),
    )
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(gate.run_completion_gate(_ctx(sid, goal="g")))
    assert not d.block  # confident block ends the turn, surfaces the blocker
    assert d.frame["goal_eval"]["verdict"] == "blocked"


def test_gate_cap_forces_allow(monkeypatch):
    from server.goals import gate, store

    # At the cap, the gate must not even call the evaluator.
    monkeypatch.setattr(
        "server.goals.evaluator.evaluate", lambda *a, **k: pytest.fail("evaluator ran at cap")
    )
    sid = _mk_session()
    store.set_goal(sid, "g")
    d = _run(gate.run_completion_gate(_ctx(sid, goal="g", attempt=8, max_consecutive_blocks=8)))
    assert not d.block and d.source == "cap"
