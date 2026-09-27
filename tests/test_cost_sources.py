"""Every cost row names its source and where its token counts came from."""

import asyncio
import importlib
import sqlite3
from types import SimpleNamespace

import pytest

from server.chat.engine.events import RoundResult, Usage
from server.chat.engine.local import LocalAdapter
from server.chat.engine.policy import LOCAL_POLICY
from server.chat.engine.runner import TurnContext, run_turn
from server.costs import tracker

m021 = importlib.import_module("server.migrations.021_cost_rows_source_and_provenance")


class _OneRound(LocalAdapter):
    def __init__(self, usage: Usage):
        super().__init__(
            model_key="k",
            base_url="http://x",
            system_prompt="",
            thinking=False,
            tools_enabled=False,
        )
        self._usage = usage

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": "ok"}], usage=self._usage
        )


def _turn(session_id: str, source: str, usage: Usage) -> None:
    ctx = TurnContext(
        session_id=session_id,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "q"}],
        adapter=_OneRound(usage),
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        cost_source=source,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    asyncio.run(go())


def test_the_engine_tags_each_round_with_the_turns_source():
    _turn("src-cron", "cron", Usage(input_tokens=10, output_tokens=5))
    (row,) = tracker.get_session_costs("src-cron")
    assert row["source"] == "cron"
    assert row["count_source"] == "reported" and row["estimated_fields"] == ""


def test_an_estimated_round_is_recorded_as_estimated():
    _turn(
        "src-est", "chat", Usage(input_tokens=250, output_tokens=40, estimated=("input", "output"))
    )
    (row,) = tracker.get_session_costs("src-est")
    assert row["count_source"] == "estimated"
    assert row["estimated_fields"] == "input,output"


@pytest.mark.parametrize("source", ["", "unattributed", "made-up"])
def test_record_turn_refuses_an_unlabelled_source(source):
    with pytest.raises(ValueError):
        tracker.record_turn("s", 0, "haiku", 1, 1, source=source)


def test_a_workflow_agent_is_tagged_workflow(monkeypatch):
    from server.workflows.agent_adapter import run_workflow_agent

    seen = {}

    async def _fake_run_agent(task, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            agent_id="a",
            status="completed",
            output="x",
            structured_output=None,
            usage={},
            turns_used=1,
            stop_reason="completed",
            tools_called=[],
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    asyncio.run(
        run_workflow_agent(
            "do it",
            {},
            session_id="s",
            default_model_id="m",
            effort_label=None,
            workspace_path=None,
            run_id="r",
            depth=1,
        )
    )
    assert seen["cost_source"] == "workflow"


# ── migration 021 ────────────────────────────────────────────────────────────

_OLD_TABLE = """
    CREATE TABLE session_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_number INTEGER NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0.0,
        api_duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""


def _old_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute(_OLD_TABLE)
    rows = [
        # A session whose own rounds cover its rollup: the rollup is a duplicate.
        ("s1", 0, "gpt5.6-sol", 1000, 10, 900, 0, 0.01),
        ("s1", 1, "gpt5.6-sol", 1000, 10, 950, 0, 0.01),
        ("s1", 0, "gpt5.6-sol_agent", 2000, 20, 1850, 0, 9.99),
        # A rollup with no rounds behind it: the only record of that spend.
        ("s2", 0, "opus4.8_agent", 500, 50, 0, 0, 0.5),
        ("s3", 905, "haiku", 10, 1, 0, 0, 0.0),
        ("dream", 0, "haiku", 10, 1, 0, 0, 0.0),
        ("voice-abc-1234", 0, "haiku", 10, 1, 0, 0, 0.0),
        ("s4", 0, "gpt6-astra", 0, 25, 0, 0, 0.0),
    ]
    conn.executemany(
        "INSERT INTO session_costs (session_id, turn_number, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _rows(conn, table="session_costs"):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_migration_021_moves_duplicate_rollups_and_labels_history(tmp_path):
    conn = _old_db(tmp_path)
    m021.migrate(conn)
    conn.commit()

    live = _rows(conn)
    backup = _rows(conn, m021.BACKUP_TABLE)
    assert [r["model"] for r in backup] == ["gpt5.6-sol_agent"]
    assert backup[0]["cost_usd"] == pytest.approx(9.99)  # kept as it was
    assert all(not r["model"].endswith("_agent") for r in live)

    by_session = {}
    for r in live:
        by_session.setdefault(r["session_id"], []).append(r)
    # The session's own rounds still count, once.
    assert sum(r["input_tokens"] for r in by_session["s1"]) == 2000
    (sole,) = by_session["s2"]
    assert (sole["model"], sole["source"]) == ("opus4.8", "agent")
    assert by_session["s3"][0]["source"] == "memory"
    assert by_session["dream"][0]["source"] == "memory"
    assert by_session["voice-abc-1234"][0]["source"] == "voice"
    assert {r["source"] for r in by_session["s1"]} == {"unattributed"}
    est = by_session["s4"][0]
    assert (est["count_source"], est["estimated_fields"]) == ("estimated", "input,output")
    assert {r["count_source"] for r in by_session["s1"]} == {"reported"}


def test_migration_021_reads_review_rows_by_the_forks_own_turn_base():
    from server.memory.review_fork import REVIEW_TURN_BASE

    assert m021.REVIEW_TURN_BASE == REVIEW_TURN_BASE


def test_migration_021_leaves_a_current_table_alone(tmp_path):
    conn = _old_db(tmp_path)
    m021.migrate(conn)
    conn.commit()
    before = _rows(conn)
    m021.migrate(conn)
    conn.commit()
    assert _rows(conn) == before
