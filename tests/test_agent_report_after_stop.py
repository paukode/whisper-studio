"""A report from agents the user stopped is kept, but wakes nobody.

When the turn that launched a team or a spawned agent is stopped, the members
are stopped too and their salvage reports land as an agent_report row the
next turn reads. That row must not start a wake turn: an answer appearing in
a session right after the user pressed Stop is one they did not ask for.
Reports from agents that ran on their own (detached, resumed) still wake the
parent, since nobody stopped anything there.
"""

import asyncio

import pytest

from server.agents import wake


@pytest.fixture
def rows_and_wakes(monkeypatch):
    rows: list = []
    wakes: list = []
    monkeypatch.setattr(
        "server.tasks.events.emit_session_event",
        lambda sid, **kw: rows.append((sid, kw)),
    )
    monkeypatch.setattr(wake, "maybe_wake", lambda sid, payload: wakes.append((sid, payload)))
    monkeypatch.setattr("server.notifications.record_notification", lambda **kw: None)
    return rows, wakes


def _agent_reports(rows):
    return [kw["payload"] for _, kw in rows if kw.get("role") == "agent_report"]


def test_a_stopped_team_keeps_its_reports_without_waking_the_parent(monkeypatch, rows_and_wakes):
    from server.agent_tools import spawn as spawn_mod
    from server.agent_tools import teams

    rows, wakes = rows_and_wakes
    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    monkeypatch.setattr(teams, "SALVAGE_WAIT_S", 0.2)
    monkeypatch.setattr(teams, "team_manifest", lambda *a, **k: None)

    async def _hanging_run_agent(task, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("server.agents.runtime.run_agent", _hanging_run_agent)

    async def _run():
        turn = asyncio.create_task(
            teams.execute_team_create(
                {"team_name": "research", "agents": [{"name": "a", "task": "find it"}]},
                "model-x",
                "stopped-sess",
            )
        )
        await asyncio.sleep(0.1)
        turn.cancel()  # the user pressed Stop on the launching turn
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(_run())
    reports = _agent_reports(rows)
    assert len(reports) == 1 and reports[0]["team_name"] == "research"
    assert all(sid == "stopped-sess" for sid, _ in rows)
    assert wakes == []


def test_a_stopped_spawn_keeps_its_report_without_waking_the_parent(monkeypatch, rows_and_wakes):
    from server.agent_tools import spawn as spawn_mod

    rows, wakes = rows_and_wakes
    monkeypatch.setattr(
        "server.agents.journal.load",
        lambda agent_id, session_id=None: {"report": "FINDINGS: half done.", "meta": {}},
    )

    async def _run():
        run_task = asyncio.ensure_future(asyncio.sleep(0))
        await run_task
        await spawn_mod._deliver_orphaned_spawn(
            "stopped-sess", "team-1", "scout", "general", "look around", "ag1", run_task
        )

    asyncio.run(_run())
    reports = _agent_reports(rows)
    assert len(reports) == 1 and "FINDINGS: half done." in reports[0]["agents"][0]["result"]
    assert wakes == []


def test_a_background_agent_report_still_wakes_the_parent(rows_and_wakes):
    from server.tasks.events import emit_agent_report

    rows, wakes = rows_and_wakes
    payload = {"team_name": "Background agent: x", "agents": [{"name": "general"}]}
    emit_agent_report("idle-sess", payload)
    assert len(_agent_reports(rows)) == 1
    assert [sid for sid, _ in wakes] == ["idle-sess"]
