"""A cron run lease is closed once, by the first writer, and a job reads as
'running' only while a run is live.

Before this, finish_run had no ``status='running'`` guard and _push_result
swallowed a failed write, so a lease could stay 'running' in history until
the next restart: the Settings list showed the job running while Stop
answered "job is not running".
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import server.cron_history as H
import server.cron_scheduler as C
from server.infrastructure.run_lease import finish_if_running
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block


@pytest.fixture
def hist(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "DB_PATH", str(tmp_path / "hist.db"))
    return H


def _status(run_id: str, job_id: str) -> dict:
    return next(r for r in H.list_runs(job_id) if r["run_id"] == run_id)


def test_finish_run_first_writer_wins(hist):
    hist.start_run("r1", "j1", "job", "s")
    assert hist.finish_run("r1", status="stopped", text="stopped by user") is True
    # A second writer for the same run (a late natural finish, the backstop)
    # changes nothing.
    assert hist.finish_run("r1", status="ok", text="done") is False
    row = _status("r1", "j1")
    assert row["status"] == "stopped"
    assert row["text"] == "stopped by user"


def test_finish_run_does_not_reopen_a_reconciled_lease(hist):
    hist.start_run("r2", "j2", "job", "s")
    assert hist.reconcile_interrupted() == 1
    assert hist.finish_run("r2", status="ok", text="late") is False
    assert _status("r2", "j2")["status"] == "failed"


def test_finish_if_running_refuses_non_identifiers():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, status TEXT)")
    with pytest.raises(ValueError):
        finish_if_running(conn, "t; DROP TABLE t", "id", "x", {"status": "ok"})
    with pytest.raises(ValueError):
        finish_if_running(conn, "t", "id", "x", {})
    conn.execute("INSERT INTO t VALUES ('x', 'running')")
    assert finish_if_running(conn, "t", "id", "x", {"status": "ok"}) is True
    assert finish_if_running(conn, "t", "id", "x", {"status": "failed"}) is False
    assert conn.execute("SELECT status FROM t").fetchone()[0] == "ok"


def test_run_state_never_reports_an_orphan_lease_as_running(hist):
    hist.start_run("r3", "j3", "job", "s")
    # History says running, but no run of j3 is live in this process: Stop
    # would refuse, so the list must not offer it.
    assert C.run_state("j3") == "failed"
    assert C.mark_running("j3", None) is True
    try:
        assert C.run_state("j3") == "running"
    finally:
        C.mark_done("j3", "r3")
    # mark_done closed the orphan lease it was handed.
    assert _status("r3", "j3")["status"] == "failed"
    assert C.run_state("never-ran") is None


def test_failed_outcome_write_is_closed_by_the_backstop(monkeypatch, hist):
    """_push_result's history write fails: the run still ends with its lease
    closed, before the job leaves the in-progress set."""
    import server.chat.tool_pool as TP
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.workspace as WS

    job = {
        "id": "job-lease",
        "name": "lease-job",
        "prompt": "do it",
        "session_id": "sess-lease",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }
    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(TP, "assemble_partitioned_pool", lambda **k: ([], [], 0))
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "feature_flags": {"cron_verify": False},
        },
    )
    monkeypatch.setattr(C, "_resolve_target_session", lambda sid: "")
    monkeypatch.setattr(C, "_emit_cron_event", lambda *a, **k: None)

    real_finish = H.finish_run
    calls: list[str] = []

    def flaky_finish(run_id, **kw):
        calls.append(kw["status"])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        # The backstop runs while the job is still marked in progress.
        assert C.run_state("job-lease") == "running"
        return real_finish(run_id, **kw)

    monkeypatch.setattr(H, "finish_run", flaky_finish)

    asyncio.run(C._execute_cron_prompt(job["id"]))

    assert calls == ["ok", "failed"]
    runs = H.list_runs("job-lease")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["text"] == C.UNRECORDED_OUTCOME
    assert C.run_state("job-lease") == "failed"
