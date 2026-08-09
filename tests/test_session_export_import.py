"""GET /api/sessions/{id}/export + POST /api/sessions/import round-trip.

Export streams a portable JSONL (one export_meta line, then one line per
transcript segment, then one line per chat_history message); import parses
that back into a brand-new session row. The round trip must reproduce
chat_history/segments/speakerNames exactly — only the session id and
created/updated timestamps are new.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    return TestClient(app)


def _seed(client: TestClient, sid: str, **extra) -> None:
    r = client.put(
        f"/api/sessions/{sid}",
        json={
            "id": sid,
            "title": f"export {sid}",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "segments": [],
            "chatHistory": [],
            "speakerNames": {},
            **extra,
        },
    )
    assert r.status_code == 200, r.text


def test_export_returns_meta_line_then_segments_then_messages():
    client = _client()
    sid = "export-shape-src"
    _seed(
        client,
        sid,
        segments=[
            {"id": "s1", "speaker": "SPEAKER_00", "text": "hello", "timestamp": 1, "edited": False},
        ],
        chatHistory=[
            {"role": "user", "content": "hi there", "timestamp": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "hello!", "timestamp": "2026-01-01T00:00:02Z"},
        ],
        speakerNames={"SPEAKER_00": "Alice"},
    )
    try:
        r = client.get(f"/api/sessions/{sid}/export")
        assert r.status_code == 200, r.text
        assert "application/x-ndjson" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]

        lines = [json.loads(line) for line in r.text.strip().splitlines()]
        assert lines[0]["type"] == "export_meta"
        assert lines[0]["session_id"] == sid
        assert lines[0]["title"] == f"export {sid}"
        assert lines[0]["speaker_names"] == {"SPEAKER_00": "Alice"}
        assert "exported_at" in lines[0]

        rest = lines[1:]
        assert [line["type"] for line in rest] == ["segment", "message", "message"]
        assert rest[0]["text"] == "hello"
        assert rest[1]["role"] == "user" and rest[1]["content"] == "hi there"
        assert rest[2]["role"] == "assistant" and rest[2]["content"] == "hello!"
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_export_missing_session_is_404():
    client = _client()
    assert client.get("/api/sessions/does-not-exist/export").status_code == 404


def test_import_round_trip_reproduces_chat_history():
    client = _client()
    sid = "export-roundtrip-src"
    new_id = None
    _seed(
        client,
        sid,
        segments=[
            {"id": "s1", "speaker": "SPEAKER_00", "text": "hello", "timestamp": 1, "edited": False},
            {"id": "s2", "speaker": "SPEAKER_01", "text": "world", "timestamp": 2, "edited": True},
        ],
        chatHistory=[
            {"role": "user", "content": "what is 2+2?", "timestamp": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "4", "timestamp": "2026-01-01T00:00:02Z"},
        ],
        speakerNames={"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"},
    )
    try:
        export_resp = client.get(f"/api/sessions/{sid}/export")
        assert export_resp.status_code == 200, export_resp.text
        jsonl = export_resp.text

        import_resp = client.post(
            "/api/sessions/import",
            content=jsonl.encode("utf-8"),
            headers={"content-type": "application/x-ndjson"},
        )
        assert import_resp.status_code == 200, import_resp.text
        body = import_resp.json()
        new_id = body["new_session_id"]
        assert new_id != sid
        assert body["title"] == f"export {sid}"

        original = client.get(f"/api/sessions/{sid}").json()
        imported = client.get(f"/api/sessions/{new_id}").json()

        assert imported["chatHistory"] == original["chatHistory"]
        assert imported["segments"] == original["segments"]
        assert imported["speakerNames"] == original["speakerNames"]
        assert imported["title"] == original["title"]
        assert imported["id"] != original["id"]
    finally:
        client.delete(f"/api/sessions/{sid}")
        if new_id:
            client.delete(f"/api/sessions/{new_id}")


def test_import_via_multipart_file_upload():
    client = _client()
    sid = "export-multipart-src"
    new_id = None
    _seed(
        client,
        sid,
        chatHistory=[{"role": "user", "content": "hey", "timestamp": "2026-01-01T00:00:01Z"}],
    )
    try:
        jsonl = client.get(f"/api/sessions/{sid}/export").text
        r = client.post(
            "/api/sessions/import",
            files={"file": ("session-export.jsonl", jsonl, "application/x-ndjson")},
        )
        assert r.status_code == 200, r.text
        new_id = r.json()["new_session_id"]
        imported = client.get(f"/api/sessions/{new_id}").json()
        assert imported["chatHistory"] == [
            {"role": "user", "content": "hey", "timestamp": "2026-01-01T00:00:01Z"}
        ]
    finally:
        client.delete(f"/api/sessions/{sid}")
        if new_id:
            client.delete(f"/api/sessions/{new_id}")


def test_import_rejects_non_export_payload():
    client = _client()
    r = client.post(
        "/api/sessions/import",
        content=json.dumps({"hello": "world"}).encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 400
    assert "export_meta" in r.json()["error"]


def test_import_rejects_empty_body():
    client = _client()
    r = client.post(
        "/api/sessions/import",
        content=b"",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 400
