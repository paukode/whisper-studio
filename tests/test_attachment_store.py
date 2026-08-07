"""Durable session-scoped attachment storage (server/attachment_store.py).

Attachments used to live only in an in-memory dict with a 3-hour TTL, so a
file silently left the conversation on the next turn and died with every
restart. The store makes them session state: write-through on upload, bound
to a session on first use, 30-day sliding retention, deleted with the session.
"""

import time
import uuid

from server import attachment_store
from server.infrastructure.sessions import _delete_session_sync, _ensure_db


def _purge_session_attachments(session_id: str) -> None:
    """Test cleanup: drop the rows this test bound to its session (production
    session deletion does this inline in sessions._delete_session_sync)."""
    with attachment_store._get_conn() as conn:
        conn.execute("DELETE FROM attachments WHERE session_id = ?", (session_id,))


def _doc_record(filename="notes.md", text="# Title\nbody"):
    return {
        "kind": "document",
        "filename": filename,
        "text": text,
        "outline": "1. Title",
        "sections": [{"n": 1, "start": 0}],
        "created": time.time(),
    }


def _image_record(filename="shot.png"):
    return {
        "kind": "image",
        "filename": filename,
        "media_type": "image/png",
        "data": "aGVsbG8=",
        "ocr_text": "hello",
        "created": time.time(),
    }


def test_document_round_trip():
    aid = str(uuid.uuid4())
    attachment_store.save_attachment(aid, _doc_record())
    rec = attachment_store.get_attachment(aid)
    assert rec is not None
    assert rec["kind"] == "document"
    assert rec["filename"] == "notes.md"
    assert rec["text"] == "# Title\nbody"
    assert rec["outline"] == "1. Title"
    assert rec["sections"] == [{"n": 1, "start": 0}]


def test_image_round_trip_keeps_ocr_and_data():
    aid = str(uuid.uuid4())
    attachment_store.save_attachment(aid, _image_record())
    rec = attachment_store.get_attachment(aid)
    assert rec is not None
    assert rec["kind"] == "image"
    assert rec["ocr_text"] == "hello"
    assert rec["data"] == "aGVsbG8="
    assert rec["media_type"] == "image/png"


def test_video_frames_round_trip():
    aid = str(uuid.uuid4())
    rec_in = _doc_record(filename="clip.mp4", text="transcript")
    rec_in["frames"] = [{"media_type": "image/jpeg", "data": "ZnJhbWU="}]
    attachment_store.save_attachment(aid, rec_in)
    rec = attachment_store.get_attachment(aid)
    assert rec["frames"] == [{"media_type": "image/jpeg", "data": "ZnJhbWU="}]


def test_get_missing_returns_none():
    assert attachment_store.get_attachment("no-such-id") is None


def test_bind_and_session_load():
    sid = f"attstore-{uuid.uuid4()}"
    a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
    attachment_store.save_attachment(a1, _doc_record())
    attachment_store.save_attachment(a2, _image_record())

    # Unbound: not visible on any session yet.
    assert attachment_store.load_session_attachments(sid) == {}

    attachment_store.bind_to_session([a1, a2], sid)
    loaded = attachment_store.load_session_attachments(sid)
    assert set(loaded) == {a1, a2}
    # Default load skips binary payloads (tool access doesn't need them).
    assert "data" not in loaded[a2]
    with_bin = attachment_store.load_session_attachments(sid, include_binary=True)
    assert with_bin[a2]["data"] == "aGVsbG8="

    # Binding to another session is a no-op once owned.
    attachment_store.bind_to_session([a1], "some-other-session")
    assert a1 in attachment_store.load_session_attachments(sid)
    assert attachment_store.load_session_attachments("some-other-session") == {}


def test_bind_slides_retention_window():
    sid = f"attstore-{uuid.uuid4()}"
    aid = str(uuid.uuid4())
    old = time.time() - attachment_store.RETENTION_SECONDS + 60
    rec = _doc_record()
    rec["created"] = old
    attachment_store.save_attachment(aid, rec)
    attachment_store.bind_to_session([aid], sid)

    # Age last_used to nearly expired, then reference it again: the rebind
    # bumps last_used, so a GC well past the ORIGINAL window keeps it.
    with attachment_store._get_conn() as conn:
        conn.execute("UPDATE attachments SET last_used = ? WHERE id = ?", (old, aid))
    attachment_store.bind_to_session([aid], sid)
    attachment_store.gc(time.time() + 120)
    assert attachment_store.get_attachment(aid) is not None
    _purge_session_attachments(sid)


def test_gc_sweeps_unbound_after_ttl_and_bound_after_retention():
    sid = f"attstore-{uuid.uuid4()}"
    fresh_unbound, stale_unbound = str(uuid.uuid4()), str(uuid.uuid4())
    fresh_bound, stale_bound = str(uuid.uuid4()), str(uuid.uuid4())
    now = time.time()

    for aid in (fresh_unbound, stale_unbound, fresh_bound, stale_bound):
        attachment_store.save_attachment(aid, _doc_record())
    attachment_store.bind_to_session([fresh_bound, stale_bound], sid)

    with attachment_store._get_conn() as conn:
        conn.execute(
            "UPDATE attachments SET created = ? WHERE id = ?",
            (now - attachment_store.UNBOUND_TTL_SECONDS - 60, stale_unbound),
        )
        conn.execute(
            "UPDATE attachments SET last_used = ? WHERE id = ?",
            (now - attachment_store.RETENTION_SECONDS - 60, stale_bound),
        )

    attachment_store.gc(now)
    assert attachment_store.get_attachment(stale_unbound) is None
    assert attachment_store.get_attachment(stale_bound) is None
    assert attachment_store.get_attachment(fresh_unbound) is not None
    assert attachment_store.get_attachment(fresh_bound) is not None
    _purge_session_attachments(sid)


def test_session_delete_cascades_to_attachments():
    _ensure_db()
    sid = f"attstore-{uuid.uuid4()}"
    aid = str(uuid.uuid4())
    attachment_store.save_attachment(aid, _doc_record())
    attachment_store.bind_to_session([aid], sid)
    assert attachment_store.load_session_attachments(sid)

    _delete_session_sync(sid)
    assert attachment_store.load_session_attachments(sid) == {}
    assert attachment_store.get_attachment(aid) is None
