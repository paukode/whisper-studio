"""How an agent run ended, in one vocabulary (server/agents/outcome.py).

The stop reason is the only outcome a run sets; its status follows from it,
so the result, the journal, the task row and its event can never disagree.
A limit stop used to read "completed" in the task list while the live card
said "turn limit".
"""

from __future__ import annotations

import pytest

from server.agents import journal as journal_mod
from server.agents.outcome import LIMIT_REASONS, STOP_REASONS, run_status
from server.agents.runtime import AgentResult
from server.tasks import registry


@pytest.mark.parametrize("reason", STOP_REASONS)
def test_every_stop_reason_has_one_status(reason):
    result = AgentResult(agent_id="a", agent_type="general", output="", stop_reason=reason)
    assert result.status == run_status(reason)
    assert result.stopped_early is (reason in LIMIT_REASONS)
    # Partial work is never reported as finished.
    assert (result.status == "completed") is (reason == "completed")


def test_an_unknown_reason_never_reads_as_finished():
    assert run_status("something new") == "failed"
    assert run_status(None) == "failed"


@pytest.mark.parametrize(
    ("reason", "status", "event"),
    [
        ("completed", "completed", "task_completed"),
        ("turn_limit", "stopped", "task_stopped"),
        ("cancelled", "stopped", "task_stopped"),
        ("error", "failed", "task_failed"),
    ],
)
def test_the_journal_row_and_event_follow_the_stop_reason(monkeypatch, reason, status, event):
    emitted = []
    monkeypatch.setattr(
        "server.tasks.events.emit_task_event",
        lambda session_id, name, row: emitted.append((name, row["status"])),
    )
    j = journal_mod.AgentJournal.open(
        f"outcome-{reason}", session_id="s-outcome", task="t", agent_type="general", model="m"
    )
    j.finish(stop_reason=reason, report="r", turns_used=3)

    meta = journal_mod.load(f"outcome-{reason}", "s-outcome")["meta"]
    assert (meta["status"], meta["stop_reason"]) == (status, reason)
    assert registry.get_task(f"outcome-{reason}")["status"] == status
    assert emitted[-1] == (event, status)


def test_a_record_written_before_the_status_followed_the_reason_reads_right():
    # A limit stop once wrote status "completed" with its limit as the reason.
    row = journal_mod.record_outcome(
        {"status": "completed", "stop_reason": "turn_limit", "rounds": 9}
    )
    assert row == {"status": "stopped", "stop_reason": "turn_limit", "turns_used": 9}
    # No reason at all: the run was cut off by a cancel.
    assert journal_mod.record_outcome({})["status"] == "stopped"


def test_a_resumed_row_is_owned_by_this_process(monkeypatch):
    tid = registry.create_task("agent", session_id="s-own", title="t", task_id="own-1")
    registry.finish_task(tid, status="stopped")
    with registry._get_conn() as conn:
        conn.execute("UPDATE agent_tasks SET owner_pid=1 WHERE task_id=?", (tid,))
    monkeypatch.setattr(registry.os, "getpid", lambda: 4242)

    registry.ensure_running_task(tid, kind="agent", session_id="s-own", title="t")

    row = registry.get_task(tid)
    assert row["status"] == "running"
    assert row["owner_pid"] == 4242
