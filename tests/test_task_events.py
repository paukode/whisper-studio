"""Session-event service: persist + publish for chat rows, publish only for
task events, and the task-event payload shape."""

import asyncio
import json
import threading
import uuid

import pytest

import server.infrastructure.sessions as S
import server.tasks.events as task_events


@pytest.fixture
def temp_sessions_db(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(S, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(S, "_session_locks", {})
    S._ensure_db()
    yield


def _seed_session(sid: str) -> None:
    with S._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, segments, "
            "chat_history, speaker_names) VALUES (?, 't', '2026-01-01', '2026-01-01', "
            "'[]', '[]', '{}')",
            (sid,),
        )


def _run_with_thread_loop(monkeypatch, fn) -> None:
    """Run ``fn`` (sync, emits events) against a live loop on another thread."""
    loop = asyncio.new_event_loop()

    async def _drain():
        await asyncio.sleep(0.15)

    monkeypatch.setattr(task_events, "_server_loop", loop)
    t = threading.Thread(target=lambda: loop.run_until_complete(_drain()), daemon=True)
    t.start()
    fn()
    t.join(timeout=2.0)
    loop.close()


def _task_row(sid: str, **over) -> dict:
    row = {
        "task_id": "abc123def456",
        "kind": "shell",
        "session_id": sid,
        "title": "sleep 60",
        "status": "completed",
        "exit_code": 0,
        "result_text": "all done",
        "created_at": "2026-07-18T05:00:00Z",
        "finished_at": "2026-07-18T05:00:02Z",
        "output_path": None,
        "meta": {},
    }
    row.update(over)
    return row


def test_emit_task_event_publishes_live_and_writes_no_chat_row(temp_sessions_db, monkeypatch):
    sid = str(uuid.uuid4())
    _seed_session(sid)

    published: list[dict] = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append((s, ev)))
    _run_with_thread_loop(
        monkeypatch,
        lambda: task_events.emit_task_event(sid, "task_completed", _task_row(sid)),
    )

    # Live publish carries the discriminator + nested payload.
    assert len(published) == 1
    psid, event = published[0]
    assert psid == sid
    assert event["type"] == "task_event"
    payload = event["taskEvent"]
    assert payload["event_type"] == "task_completed"
    assert payload["task_id"] == "abc123def456"
    assert payload["kind"] == "shell"
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["duration_ms"] == 2000
    assert payload["result_tail"] == "all done"

    # The registry row is the record; the chat carries nothing for it.
    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
    assert json.loads(row["chat_history"]) == []


def test_emit_task_event_without_a_session_reaches_the_app_channel(monkeypatch):
    """A task no session owns still updates the header's list: it goes out on
    the app-wide channel (empty session id), like mcp_changed."""
    published: list = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append((s, ev)))
    task_events.emit_task_event("", "task_started", _task_row("", status="running"))
    assert published == [("", published[0][1])]
    assert published[0][1]["taskEvent"]["event_type"] == "task_started"


def test_emit_task_event_rejects_unknown_type():
    with pytest.raises(ValueError):
        task_events.emit_task_event("sid", "task_exploded", _task_row("sid"))


def test_emit_session_event_skips_empty_session(monkeypatch):
    called = []
    monkeypatch.setattr(task_events, "_server_loop", None)
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: called.append(ev))
    task_events.emit_session_event("", role="task_event", payload_key="taskEvent", payload={})
    assert called == []


def test_result_tail_truncated(monkeypatch):
    published: list = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append(ev))
    big = "x" * (task_events.RESULT_TAIL_MAX * 3)
    task_events.emit_task_event(
        "sid", "task_failed", _task_row("sid", status="failed", result_text=big, exit_code=1)
    )
    payload = published[0]["taskEvent"]
    assert len(payload["result_tail"]) == task_events.RESULT_TAIL_MAX


def test_cron_delegation_preserves_payload_shape(temp_sessions_db, monkeypatch):
    """cron_scheduler._emit_cron_event through the shared service emits the
    exact legacy payload (frontend CronEventCard contract)."""
    import server.cron_scheduler as C

    sid = str(uuid.uuid4())
    _seed_session(sid)
    published: list = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append(ev))
    _run_with_thread_loop(
        monkeypatch,
        lambda: C._emit_cron_event(
            sid,
            event_type="cron_fired",
            cron_id="j1",
            cron_name="nightly",
            text="report",
            status="ok",
            run_id="r1",
            duration_ms=1234,
        ),
    )
    assert published[0]["type"] == "cron_event"
    payload = published[0]["cronEvent"]
    assert payload["event_type"] == "cron_fired"
    assert payload["cron_id"] == "j1"
    assert payload["cron_name"] == "nightly"
    assert payload["text"] == "report"
    assert payload["status"] == "ok"
    assert payload["run_id"] == "r1"
    assert payload["duration_ms"] == 1234
    assert "timestamp" in payload
