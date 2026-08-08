"""Cross-session messaging: list_sessions / send_session_message.

Mirrors tests/test_task_events.py's fixtures — send_session_message rides
the exact same emit_session_event persist+publish path as task_event, just
with role='session_message' and a payload the backend also feeds back to the
model (see visible_chat_history / _session_message_prompt_view).
"""

import asyncio
import json
import threading
import uuid

import pytest

import server.infrastructure.sessions as S
from server.agent_tools.cross_session import (
    _is_duplicate,
    _recent_sends,
    execute_list_sessions,
    execute_send_session_message,
)


@pytest.fixture
def temp_sessions_db(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(S, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(S, "_session_locks", {})
    S._ensure_db()
    yield


@pytest.fixture(autouse=True)
def _clear_dedupe_cache():
    _recent_sends.clear()
    yield
    _recent_sends.clear()


def _seed_session(sid: str, *, title: str = "t", archived: int = 0) -> None:
    with S._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, segments, "
            "chat_history, speaker_names, archived) VALUES (?, ?, '2026-01-01', "
            "'2026-01-01', '[]', '[]', '{}', ?)",
            (sid, title, archived),
        )


def _run_with_thread_loop(monkeypatch, fn) -> None:
    """Run ``fn`` (sync, emits events) against a live loop on another thread."""
    import server.tasks.events as task_events

    loop = asyncio.new_event_loop()

    async def _drain():
        await asyncio.sleep(0.15)

    monkeypatch.setattr(task_events, "_server_loop", loop)
    t = threading.Thread(target=lambda: loop.run_until_complete(_drain()), daemon=True)
    t.start()
    fn()
    t.join(timeout=2.0)
    loop.close()


# ── list_sessions ────────────────────────────────────────────────────────


def test_list_sessions_excludes_self_cron_inbox_and_archived(temp_sessions_db):
    me = str(uuid.uuid4())
    other = str(uuid.uuid4())
    archived = str(uuid.uuid4())
    _seed_session(me, title="Mine")
    _seed_session(other, title="Other")
    _seed_session(archived, title="Old", archived=1)
    S.ensure_cron_inbox()

    result = json.loads(execute_list_sessions(me))
    ids = {s["session_id"] for s in result["sessions"]}
    assert ids == {other}
    assert result["count"] == 1


# ── send_session_message: validation ────────────────────────────────────


def test_send_rejects_missing_fields(temp_sessions_db):
    result = json.loads(execute_send_session_message({}, "sid-a"))
    assert "error" in result


def test_send_rejects_self_message(temp_sessions_db):
    sid = str(uuid.uuid4())
    _seed_session(sid)
    result = json.loads(execute_send_session_message({"to_session_id": sid, "content": "hi"}, sid))
    assert "error" in result


def test_send_rejects_unknown_target(temp_sessions_db):
    result = json.loads(
        execute_send_session_message({"to_session_id": "does-not-exist", "content": "hi"}, "sid-a")
    )
    assert "error" in result


def test_send_rejects_archived_target(temp_sessions_db):
    target = str(uuid.uuid4())
    _seed_session(target, archived=1)
    result = json.loads(
        execute_send_session_message({"to_session_id": target, "content": "hi"}, "sid-a")
    )
    assert "error" in result


# ── send_session_message: delivery ──────────────────────────────────────


def test_send_persists_row_and_publishes(temp_sessions_db, monkeypatch):
    sender = str(uuid.uuid4())
    target = str(uuid.uuid4())
    _seed_session(sender, title="Sender Session")
    _seed_session(target, title="Target Session")

    published: list[dict] = []
    from server.agents import event_bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", lambda s, ev: published.append((s, ev)))

    result_holder = {}
    _run_with_thread_loop(
        monkeypatch,
        lambda: result_holder.update(
            result=json.loads(
                execute_send_session_message(
                    {"to_session_id": target, "content": "the migration is done"}, sender
                )
            )
        ),
    )

    assert result_holder["result"]["sent"] is True
    assert result_holder["result"]["to"] == target

    assert len(published) == 1
    psid, event = published[0]
    assert psid == target
    assert event["type"] == "session_message"
    payload = event["sessionMessage"]
    assert payload["from_session_id"] == sender
    assert payload["from_title"] == "Sender Session"
    assert payload["content"] == "the migration is done"

    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (target,)).fetchone()
    history = json.loads(row["chat_history"])
    rows = [m for m in history if m.get("role") == "session_message"]
    assert len(rows) == 1
    assert rows[0]["content"] == ""
    assert rows[0]["sessionMessage"]["content"] == "the migration is done"


def test_send_dedupes_identical_repeats(temp_sessions_db):
    target = str(uuid.uuid4())
    _seed_session(target)
    first = json.loads(
        execute_send_session_message({"to_session_id": target, "content": "same text"}, "a")
    )
    second = json.loads(
        execute_send_session_message({"to_session_id": target, "content": "same text"}, "a")
    )
    assert first.get("sent") is True
    assert "error" in second


def test_is_duplicate_window():
    assert _is_duplicate("a", "b", "hello") is False
    assert _is_duplicate("a", "b", "hello") is True
    # Different content, or different pair, is not a duplicate.
    assert _is_duplicate("a", "b", "different") is False
    assert _is_duplicate("a", "c", "hello") is False


# ── prompt visibility (the model-facing side) ───────────────────────────


def test_session_message_rows_are_ui_only_but_prompt_visible():
    """The stored role never reaches the model as a literal role, but the
    content does — unlike cron_event/task_event, which are dropped outright."""
    history = [
        {"role": "user", "content": "hi"},
        {
            "role": "session_message",
            "content": "",
            "timestamp": "2026-08-08T00:00:00Z",
            "sessionMessage": {
                "from_session_id": "s1",
                "from_title": "Payments API",
                "content": "the migration is done",
            },
        },
        {"role": "assistant", "content": "hello"},
    ]
    visible = S.visible_chat_history(history)
    assert [m["role"] for m in visible] == ["user", "user", "assistant"]
    injected = visible[1]
    assert injected["role"] == "user"
    assert "Payments API" in injected["content"]
    assert "the migration is done" in injected["content"]

    # But it's still backend-owned for save-merge purposes, exactly like
    # cron_event/task_event (PROMPT_ROLES excludes the raw stored role).
    assert "session_message" not in S.PROMPT_ROLES
    ui_only = S._ui_only_rows(history)
    assert [m["role"] for m in ui_only] == ["session_message"]


def test_row_cap_applies_per_role():
    history = [{"role": "session_message", "content": "", "n": i} for i in range(60)]
    history += [{"role": "cron_event", "content": "", "n": i} for i in range(60)]
    capped = S._enforce_backend_row_caps(history)
    session_rows = [m for m in capped if m["role"] == "session_message"]
    cron_rows = [m for m in capped if m["role"] == "cron_event"]
    assert len(session_rows) == S.MAX_SESSION_MESSAGES_PER_SESSION
    assert len(cron_rows) == S.MAX_CRON_EVENTS_PER_SESSION
    # Oldest are dropped, newest survive.
    assert session_rows[0]["n"] == 60 - S.MAX_SESSION_MESSAGES_PER_SESSION
    assert session_rows[-1]["n"] == 59
