"""The header's task list and the agent pane: numbered journal events, the
record and output routes, Stop for every task kind, and the migration that
drops the retired task_event chat rows."""

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.agents import journal as journal_mod
from server.agents.journal import AgentJournal
from server.agents.routes import router as agents_router
from server.tasks import agents as task_agents
from server.tasks import registry, shell
from server.tasks.routes import router as tasks_router


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agents_router)
    app.include_router(tasks_router)
    return TestClient(app)


def _open(agent_id: str, **over) -> AgentJournal:
    kw = dict(session_id="s1", task="probe", agent_type="general", model="m", max_turns=5)
    kw.update(over)
    j = AgentJournal.open(agent_id, **kw)
    assert j is not None
    return j


# ── journal events are numbered, and a reopened record keeps counting ────────


def test_events_carry_their_line_number_as_seq():
    j = _open("a1")
    assert j.event("tool_call", tool_name="ws_grep") == 2  # "started" was 1
    assert j.event("text", text="hi") == 3
    lines = [json.loads(line) for line in open(j.events_path)]
    assert [line["seq"] for line in lines] == [1, 2, 3]
    assert lines[1]["phase"] == "tool_call"


def test_resumed_record_continues_the_numbering():
    j = _open("a2")
    j.event("text", text="first run")
    j.finish(status="completed", stop_reason="completed", report="done", turns_used=1)
    again = _open("a2", resumed=True)
    seq = again.event("text", text="second run")
    lines = [json.loads(line) for line in open(again.events_path)]
    assert seq == len(lines) == lines[-1]["seq"]


# ── the feed the pane loads ──────────────────────────────────────────────────


def test_load_feed_returns_meta_events_and_report():
    j = _open("a3")
    j.event("tool_call", tool_name="ws_grep")
    j.finish(status="completed", stop_reason="completed", report="FINDINGS: x", turns_used=1)
    feed = journal_mod.load_feed("a3")
    assert feed["agent_id"] == "a3"
    assert feed["meta"]["status"] == "completed"
    assert [e["phase"] for e in feed["events"]] == ["started", "tool_call", "finished"]
    assert feed["report"] == "FINDINGS: x"
    assert journal_mod.load_feed("nobody") is None


def test_load_feed_keeps_only_the_newest_events(monkeypatch):
    monkeypatch.setattr(journal_mod, "FEED_EVENTS_MAX", 3)
    j = _open("a4")
    for i in range(5):
        j.event("text", text=str(i))
    feed = journal_mod.load_feed("a4")
    assert [e["seq"] for e in feed["events"]] == [4, 5, 6]


def test_record_route_serves_the_feed_and_404s_unknown_agents(client):
    _open("a5").event("text", text="hello")
    r = client.get("/api/agents/a5/record")
    assert r.status_code == 200
    assert [e["phase"] for e in r.json()["events"]] == ["started", "text"]
    assert r.json()["meta"]["task"] == "probe"
    assert client.get("/api/agents/missing/record").status_code == 404


# ── a running row's output tail ──────────────────────────────────────────────


def test_output_route_tails_the_task_file(tmp_path, client):
    out = tmp_path / "out.txt"
    out.write_text("".join(f"line {i}\n" for i in range(10)))
    tid = registry.create_task("shell", session_id="s1", title="echo", output_path=str(out))
    r = client.get(f"/api/background-tasks/{tid}/output?tail_lines=3")
    assert r.status_code == 200
    assert r.json()["text"] == "line 7\nline 8\nline 9\n"
    assert r.json()["status"] == "running"
    assert client.get("/api/background-tasks/nope/output").status_code == 404


# ── Stop reaches every kind's own runner ─────────────────────────────────────


def test_stop_reaches_a_spawned_agent_through_its_team(client, monkeypatch):
    from server.agent_tools import teams

    tid = registry.create_task("agent", session_id="s1", title="probe", meta={"team_id": "team9"})

    class _Task:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    task = _Task()
    monkeypatch.setattr(teams, "_teams", {"team9": {"task": task}})
    r = client.post(f"/api/background-tasks/{tid}/stop")
    assert r.json() == {"stopped": True, "status": "stopping"}
    assert task.cancelled is True
    assert teams._teams["team9"]["stop_requested"] is True


def test_stop_of_an_agent_nobody_runs_is_refused(client):
    tid = registry.create_task("agent", session_id="s1", title="probe")
    assert task_agents.cancel_task(tid) is False
    r = client.post(f"/api/background-tasks/{tid}/stop")
    assert r.json() == {"stopped": False, "status": "running"}


def test_stop_dispatches_workflows_and_ci_watches(client, monkeypatch):
    from server.ci import manager as ci_manager
    from server.workflows import manager as wf_manager

    calls: list = []

    async def fake_stop_run(run_id):
        calls.append(("workflow", run_id))
        return True

    monkeypatch.setattr(wf_manager, "stop_run", fake_stop_run)
    monkeypatch.setattr(
        ci_manager, "stop_watch", lambda task_id: calls.append(("ci", task_id)) or True
    )
    wf = registry.create_task("workflow", session_id="s1", title="wf", meta={"run_id": "r1"})
    ci = registry.create_task("ci", session_id="s1", title="CI watch main")
    assert client.post(f"/api/background-tasks/{wf}/stop").json()["stopped"] is True
    assert client.post(f"/api/background-tasks/{ci}/stop").json()["stopped"] is True
    assert calls == [("workflow", "r1"), ("ci", ci)]


# ── the retired chat rows leave every session ────────────────────────────────


def _migration_module():
    import importlib

    return importlib.import_module("server.migrations.024_drop_task_event_rows")


def test_migration_024_rewrites_only_sessions_that_carry_the_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "sessions.db"))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, chat_history TEXT)")
    with_rows = json.dumps(
        [
            {"role": "user", "content": "hi"},
            {"role": "task_event", "content": "", "taskEvent": {"event_type": "task_started"}},
            {"role": "assistant", "content": "hello"},
            {"role": "task_event", "content": "", "taskEvent": {"event_type": "task_completed"}},
        ]
    )
    untouched = '[{"role": "user", "content": "task_event is just a word here"}]'
    broken = "{not json"
    conn.execute("INSERT INTO sessions VALUES ('a', ?)", (with_rows,))
    conn.execute("INSERT INTO sessions VALUES ('b', ?)", (untouched,))
    conn.execute("INSERT INTO sessions VALUES ('c', ?)", (broken,))
    _migration_module().migrate(conn)
    rows = dict(conn.execute("SELECT id, chat_history FROM sessions").fetchall())
    assert [m["role"] for m in json.loads(rows["a"])] == ["user", "assistant"]
    assert rows["b"] == untouched
    assert rows["c"] == broken


def test_migration_024_is_registered_in_order():
    from server.migrations.runner import _discover_migrations

    versions = [m["version"] for m in _discover_migrations()]
    assert 24 in versions
    assert versions == sorted(versions)


def test_cancel_task_prefers_the_live_coroutine():
    async def main():
        tid = registry.create_task("agent", session_id="s1", title="t", meta={"team_id": "x"})
        fut = asyncio.get_running_loop().create_future()
        task_agents._running[tid] = asyncio.ensure_future(fut)
        try:
            assert task_agents.cancel_task(tid) is True
            await asyncio.sleep(0)
            assert task_agents._running[tid].cancelled()
        finally:
            task_agents._running.pop(tid, None)

    asyncio.run(main())
