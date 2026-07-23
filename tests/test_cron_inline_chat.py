"""Tests for inline cron-event delivery into chat sessions.

Covers:
  - visible_chat_history filters cron_event rows out of the Bedrock prompt
  - _enforce_cron_event_cap drops the oldest cron_events past the cap and
    leaves user/assistant rows untouched
  - append_message handles N concurrent appends without clobbering
  - _upsert_session preserves backend-owned cron_event rows when the
    frontend submits a chat_history that doesn't include them yet
"""

import asyncio
import json
import os
import tempfile
import uuid

import pytest

import server.infrastructure.sessions as S


@pytest.fixture
def temp_sessions_db(monkeypatch):
    """Point the sessions module at a fresh temp DB. _ensure_db creates the
    full current schema, migrated columns included.
    """
    tmpdir = tempfile.mkdtemp(prefix="sessions-test-")
    monkeypatch.setattr(S, "STORAGE_DIR", tmpdir)
    monkeypatch.setattr(S, "DB_PATH", os.path.join(tmpdir, "sessions.db"))
    # Also reset any locks left over from previous tests.
    monkeypatch.setattr(S, "_session_locks", {})
    S._ensure_db()
    return tmpdir


def _seed_session(session_id: str) -> None:
    with S._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, "test", "now", "now"),
        )


def test_visible_chat_history_filters_cron_event():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "cron_event", "event_type": "cron_fired"},
        {"role": "assistant", "content": "hey"},
        {"role": "cron_event", "event_type": "cron_created"},
        {"role": "system_internal", "content": "x"},  # future role — also dropped
    ]
    filtered = S.visible_chat_history(history)
    assert [m["role"] for m in filtered] == ["user", "assistant"]


def test_visible_chat_history_passthrough_when_pure():
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert S.visible_chat_history(history) == history


def test_enforce_cron_event_cap_drops_oldest():
    history = (
        [{"role": "user", "content": "q"}]
        + [{"role": "cron_event", "i": i} for i in range(S.MAX_CRON_EVENTS_PER_SESSION + 10)]
        + [{"role": "assistant", "content": "a"}]
    )
    capped = S._enforce_cron_event_cap(history)
    cron_rows = [m for m in capped if m.get("role") == "cron_event"]
    assert len(cron_rows) == S.MAX_CRON_EVENTS_PER_SESSION
    # Oldest dropped; newest kept.
    assert cron_rows[0]["i"] == 10
    assert cron_rows[-1]["i"] == S.MAX_CRON_EVENTS_PER_SESSION + 9
    # Non-cron rows preserved at their original positions.
    assert capped[0] == {"role": "user", "content": "q"}
    assert capped[-1] == {"role": "assistant", "content": "a"}


def test_enforce_cron_event_cap_noop_under_limit():
    history = [{"role": "user", "content": "q"}, {"role": "cron_event", "i": 0}]
    assert S._enforce_cron_event_cap(history) == history


def test_append_message_concurrent_no_clobber(temp_sessions_db):
    sid = str(uuid.uuid4())
    _seed_session(sid)

    async def _go():
        n = 25
        coros = [
            S.append_message(sid, {"role": "cron_event", "i": i, "timestamp": f"t{i}"})
            for i in range(n)
        ]
        results = await asyncio.gather(*coros)
        assert all(results), "one or more appends reported a missing session"
        return n

    n = asyncio.run(_go())

    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
    history = json.loads(row["chat_history"])
    cron_rows = [m for m in history if m.get("role") == "cron_event"]
    # All N appends landed and are distinct (no clobbering under contention).
    assert len(cron_rows) == n
    assert {m["i"] for m in cron_rows} == set(range(n))


def test_append_message_upserts_missing_session(temp_sessions_db):
    """append_message used to drop the message when the session row
    didn't exist (returning False). That dropped chip rows when an
    out-of-band writer raced the frontend's fire-and-forget session
    create. The current race-safe behaviour upserts a placeholder
    row so the message lands; the next session save fills in the
    title and metadata. The function returns True after the upsert.
    """
    msg = {
        "role": "cron_event",
        "event_type": "cron_fired",
        "cron_name": "x",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    result = asyncio.run(S.append_message("does-not-exist", msg))
    assert result is True

    # And the row actually landed in chat_history of the upserted session.
    import json

    with S._get_conn() as conn:
        row = conn.execute(
            "SELECT chat_history, title FROM sessions WHERE id = ?",
            ("does-not-exist",),
        ).fetchone()
    assert row is not None
    history = json.loads(row["chat_history"])
    assert any(m.get("role") == "cron_event" for m in history)
    # Placeholder title — next session save will replace this.
    assert row["title"] == "New Session"


def test_upsert_session_preserves_backend_cron_events(temp_sessions_db):
    """The frontend's PUT /api/sessions UPSERT must not clobber cron_event
    rows that the cron daemon appended while the user wasn't typing.
    """
    sid = str(uuid.uuid4())
    _seed_session(sid)

    # Simulate the daemon: append 5 cron_events directly into chat_history.
    cron_rows = [
        {"role": "cron_event", "event_type": "cron_fired", "cron_name": "x", "timestamp": f"t{i}"}
        for i in range(5)
    ]
    with S._get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET chat_history = ? WHERE id = ?",
            (json.dumps(cron_rows), sid),
        )

    # Now the frontend saves with prompt-only history (cron_events not yet
    # hydrated into its local store).
    frontend_history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    S._upsert_session(
        sid,
        title="test",
        custom_title=0,
        generated_title=0,
        created_at="now",
        updated_at="now",
        segments=json.dumps([]),
        chat_history_frontend=frontend_history,
        speaker_names=json.dumps({}),
        workspace_path="",
        compaction_count=0,
        latched_config=json.dumps({}),
    )

    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
    merged = json.loads(row["chat_history"])
    cron_after = [m for m in merged if m.get("role") == "cron_event"]
    assert len(cron_after) == 5, "cron_events were clobbered by frontend save"
    # Frontend's prompt rows still come first.
    assert merged[0]["role"] == "user"
    assert merged[1]["role"] == "assistant"


def test_emit_cron_event_writes_nested_payload(temp_sessions_db, monkeypatch):
    """``_emit_cron_event`` must persist a row that matches the frontend
    ChatMessage shape: ``cronEvent`` nested, ``content`` set to empty
    string, outer ``timestamp`` mirroring the payload. Anything else
    breaks the CronEventCard guard on session resume.
    """
    import server.cron_scheduler as C

    sid = str(uuid.uuid4())
    _seed_session(sid)

    # Bridge the captured loop so the threadsafe dispatch lands on a
    # real loop we can drain synchronously.
    loop = asyncio.new_event_loop()

    async def _drain():
        # Yield once so any pending callbacks scheduled via
        # call_soon_threadsafe / run_coroutine_threadsafe finish.
        await asyncio.sleep(0.1)

    def _runner():
        loop.run_until_complete(_drain())

    # The persist+publish path now lives in the generalized session-event
    # service; the loop is captured there, not in cron_scheduler.
    import server.tasks.events as task_events

    monkeypatch.setattr(task_events, "_server_loop", loop)

    # Run the loop in a thread so _emit_cron_event (sync) can dispatch
    # coroutines onto it without blocking us.
    import threading

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    C._emit_cron_event(
        sid,
        event_type="cron_fired",
        cron_id="abc123",
        cron_name="sagemaker-monitor",
        interval_minutes=30,
        text="## Status\n\nAll endpoints healthy.",
        status="ok",
        run_id="run-1",
    )

    t.join(timeout=2.0)
    loop.close()

    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
    history = json.loads(row["chat_history"])
    cron_rows = [m for m in history if m.get("role") == "cron_event"]
    assert len(cron_rows) == 1, f"expected one row, got {len(cron_rows)}"

    row = cron_rows[0]
    # Outer envelope must match the frontend ChatMessage shape.
    assert row["role"] == "cron_event"
    assert row["content"] == "", "content must be empty string (not undefined)"
    assert "timestamp" in row, "outer timestamp required for upsert dedup"

    # Nested payload carries all the cron fields.
    payload = row["cronEvent"]
    assert payload["event_type"] == "cron_fired"
    assert payload["cron_id"] == "abc123"
    assert payload["cron_name"] == "sagemaker-monitor"
    assert payload["interval_minutes"] == 30
    assert payload["text"].startswith("## Status")
    assert payload["status"] == "ok"
    assert payload["run_id"] == "run-1"
    assert payload["timestamp"] == row["timestamp"], (
        "outer/inner timestamp must match so upsert dedup keys line up"
    )


def test_upsert_session_deduplicates_already_known_rows(temp_sessions_db):
    """If the frontend has already hydrated some cron_event rows (with
    timestamps) and sends them back, the merge must not double-append.
    """
    sid = str(uuid.uuid4())
    _seed_session(sid)

    shared = {
        "role": "cron_event",
        "event_type": "cron_fired",
        "cron_name": "x",
        "timestamp": "shared-ts",
    }
    backend_only = {
        "role": "cron_event",
        "event_type": "cron_fired",
        "cron_name": "y",
        "timestamp": "backend-only-ts",
    }
    with S._get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET chat_history = ? WHERE id = ?",
            (json.dumps([shared, backend_only]), sid),
        )

    # Frontend re-sends `shared` (it already knows about it) plus prompt rows.
    frontend_history = [
        {"role": "user", "content": "q"},
        shared,
        {"role": "assistant", "content": "a"},
    ]
    S._upsert_session(
        sid,
        title="test",
        custom_title=0,
        generated_title=0,
        created_at="now",
        updated_at="now",
        segments=json.dumps([]),
        chat_history_frontend=frontend_history,
        speaker_names=json.dumps({}),
        workspace_path="",
        compaction_count=0,
        latched_config=json.dumps({}),
    )

    with S._get_conn() as conn:
        row = conn.execute("SELECT chat_history FROM sessions WHERE id = ?", (sid,)).fetchone()
    merged = json.loads(row["chat_history"])
    cron_rows = [m for m in merged if m.get("role") == "cron_event"]
    # Exactly two: the shared one (from frontend) and the backend-only one.
    timestamps = sorted(m["timestamp"] for m in cron_rows)
    assert timestamps == ["backend-only-ts", "shared-ts"], (
        f"merge produced duplicates or lost rows: {timestamps}"
    )
