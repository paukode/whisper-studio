"""GET /api/sessions/{id}/export + POST /api/sessions/import-transcript round-trip,
plus their multi-session counterparts, bulk-export (many sessions -> one zip)
and bulk-import (many files, or that same zip, -> many new sessions).

Export streams a portable JSONL (one export_meta line, then one line per
transcript segment, then one line per chat_history message); import parses
that back into a brand-new session row. The round trip must reproduce
chat_history/segments/speakerNames exactly — only the session id and
created/updated timestamps are new. Bulk export/import must round-trip the
exact same way, just N sessions at a time.
"""

import io
import json
import zipfile

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
            "/api/sessions/import-transcript",
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
            "/api/sessions/import-transcript",
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
        "/api/sessions/import-transcript",
        content=json.dumps({"hello": "world"}).encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 400
    assert "export_meta" in r.json()["error"]


def test_import_rejects_empty_body():
    client = _client()
    r = client.post(
        "/api/sessions/import-transcript",
        content=b"",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 400


# ── Bulk export/import (sidebar multi-select) ────────────────────────────


def test_bulk_export_zips_one_jsonl_per_session():
    client = _client()
    sids = ["bulk-export-a", "bulk-export-b"]
    for sid in sids:
        _seed(
            client,
            sid,
            chatHistory=[{"role": "user", "content": f"hi from {sid}", "timestamp": "t"}],
        )
    try:
        r = client.post("/api/sessions/bulk-export", json={"ids": sids})
        assert r.status_code == 200, r.text
        assert "application/zip" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            assert len(names) == 2
            assert all(n.endswith(".jsonl") for n in names)
            contents = {n: zf.read(n).decode("utf-8") for n in names}
        bodies = [json.loads(line) for c in contents.values() for line in c.strip().splitlines()]
        titles = {obj["title"] for obj in bodies if obj["type"] == "export_meta"}
        assert titles == {f"export {sid}" for sid in sids}
        messages = [obj["content"] for obj in bodies if obj["type"] == "message"]
        assert set(messages) == {f"hi from {sid}" for sid in sids}
    finally:
        for sid in sids:
            client.delete(f"/api/sessions/{sid}")


def test_bulk_export_dedupes_same_titled_sessions_into_distinct_zip_entries():
    client = _client()
    sids = ["bulk-export-dup-1", "bulk-export-dup-2"]
    for sid in sids:
        client.put(
            f"/api/sessions/{sid}",
            json={
                "id": sid,
                "title": "Same Title",
                "segments": [],
                "chatHistory": [],
                "speakerNames": {},
            },
        )
    try:
        r = client.post("/api/sessions/bulk-export", json={"ids": sids})
        assert r.status_code == 200, r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert len(names) == 2
        assert len(set(names)) == 2  # distinct filenames, not one overwriting the other
    finally:
        for sid in sids:
            client.delete(f"/api/sessions/{sid}")


def test_bulk_export_skips_unknown_ids_but_keeps_known_ones():
    client = _client()
    sid = "bulk-export-known"
    _seed(client, sid)
    try:
        r = client.post("/api/sessions/bulk-export", json={"ids": [sid, "does-not-exist"]})
        assert r.status_code == 200, r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert len(zf.namelist()) == 1
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_bulk_export_all_unknown_ids_is_404():
    client = _client()
    r = client.post("/api/sessions/bulk-export", json={"ids": ["nope-1", "nope-2"]})
    assert r.status_code == 404


def test_bulk_export_rejects_malformed_ids():
    client = _client()
    assert client.post("/api/sessions/bulk-export", json={"ids": "not-a-list"}).status_code == 400
    assert client.post("/api/sessions/bulk-export", json={"ids": [1, 2]}).status_code == 400
    assert client.post("/api/sessions/bulk-export", json={"ids": ["x"] * 501}).status_code == 400


def test_bulk_import_multiple_plain_jsonl_files_creates_multiple_sessions():
    client = _client()
    sids = ["bulk-import-src-a", "bulk-import-src-b"]
    for sid in sids:
        _seed(
            client, sid, chatHistory=[{"role": "user", "content": f"msg {sid}", "timestamp": "t"}]
        )
    new_ids = []
    try:
        exports = [client.get(f"/api/sessions/{sid}/export").text for sid in sids]
        r = client.post(
            "/api/sessions/bulk-import",
            files=[
                ("files", (f"{sid}.jsonl", body, "application/x-ndjson"))
                for sid, body in zip(sids, exports, strict=True)
            ],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["imported"]) == 2
        assert body["failed"] == []
        new_ids = [entry["new_session_id"] for entry in body["imported"]]
        assert len(set(new_ids)) == 2
        imported_titles = {client.get(f"/api/sessions/{nid}").json()["title"] for nid in new_ids}
        assert imported_titles == {f"export {sid}" for sid in sids}
    finally:
        for sid in sids:
            client.delete(f"/api/sessions/{sid}")
        for nid in new_ids:
            client.delete(f"/api/sessions/{nid}")


def test_bulk_import_a_bulk_export_zip_round_trips_every_session():
    client = _client()
    sids = ["bulk-roundtrip-a", "bulk-roundtrip-b", "bulk-roundtrip-c"]
    for sid in sids:
        _seed(
            client,
            sid,
            chatHistory=[{"role": "user", "content": f"content {sid}", "timestamp": "t"}],
        )
    new_ids = []
    try:
        zip_bytes = client.post("/api/sessions/bulk-export", json={"ids": sids}).content
        r = client.post(
            "/api/sessions/bulk-import",
            files=[("files", ("sessions-export.zip", zip_bytes, "application/zip"))],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["imported"]) == 3
        assert body["failed"] == []
        new_ids = [entry["new_session_id"] for entry in body["imported"]]
        imported_titles = {client.get(f"/api/sessions/{nid}").json()["title"] for nid in new_ids}
        assert imported_titles == {f"export {sid}" for sid in sids}
    finally:
        for sid in sids:
            client.delete(f"/api/sessions/{sid}")
        for nid in new_ids:
            client.delete(f"/api/sessions/{nid}")


def test_bulk_import_reports_per_file_failures_without_failing_the_batch():
    client = _client()
    sid = "bulk-import-good"
    _seed(client, sid)
    new_ids = []
    try:
        good = client.get(f"/api/sessions/{sid}/export").text
        r = client.post(
            "/api/sessions/bulk-import",
            files=[
                ("files", ("good.jsonl", good, "application/x-ndjson")),
                ("files", ("bad.jsonl", "not json export data", "text/plain")),
                ("files", ("empty.jsonl", "", "text/plain")),
            ],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["imported"]) == 1
        assert len(body["failed"]) == 2
        assert {f["filename"] for f in body["failed"]} == {"bad.jsonl", "empty.jsonl"}
        new_ids = [entry["new_session_id"] for entry in body["imported"]]
    finally:
        client.delete(f"/api/sessions/{sid}")
        for nid in new_ids:
            client.delete(f"/api/sessions/{nid}")


def test_bulk_import_zip_with_no_jsonl_entries_is_reported_as_a_failure():
    client = _client()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a session export")
    r = client.post(
        "/api/sessions/bulk-import",
        files=[("files", ("empty-bundle.zip", buf.getvalue(), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "empty-bundle.zip"


def test_bulk_import_corrupt_zip_is_reported_as_a_failure_not_a_500():
    client = _client()
    r = client.post(
        "/api/sessions/bulk-import",
        files=[("files", ("broken.zip", b"PK\x03\x04not actually a zip", "application/zip"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "broken.zip"
