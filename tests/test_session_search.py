"""GET /api/sessions/search: content search behind the sidebar toggle."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router
from server.migrations.runner import run_migrations


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    run_migrations()
    return TestClient(app)


def _seed(client: TestClient, sid: str, **extra) -> None:
    r = client.put(
        f"/api/sessions/{sid}",
        json={
            "id": sid,
            "title": f"search {sid}",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "segments": [],
            "chatHistory": [],
            "speakerNames": {},
            **extra,
        },
    )
    assert r.status_code == 200, r.text


def _search(client: TestClient, q: str) -> list[dict]:
    r = client.get("/api/sessions/search", params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def _ids(results: list[dict]) -> set[str]:
    return {r["id"] for r in results}


def test_matches_user_message_content_case_insensitive():
    client = _client()
    sid = "search-user-msg"
    _seed(
        client,
        sid,
        chatHistory=[
            {
                "role": "user",
                "content": "Remind me about the Zanzibar deployment",
                "timestamp": "t",
            },
        ],
    )
    try:
        results = _search(client, "zanzibar")
        assert sid in _ids(results)
        hit = next(r for r in results if r["id"] == sid)
        assert "Zanzibar" in hit["snippet"]
        # Title-only queries don't hit content search.
        assert sid not in _ids(_search(client, "no-such-word-anywhere"))
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_matches_assistant_block_content():
    client = _client()
    sid = "search-block-msg"
    _seed(
        client,
        sid,
        chatHistory=[
            {
                "role": "assistant",
                "timestamp": "t",
                "content": [
                    {"type": "text", "text": "The quokka population is stable."},
                ],
            },
        ],
    )
    try:
        assert sid in _ids(_search(client, "quokka"))
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_matches_transcript_segments():
    client = _client()
    sid = "search-transcript"
    _seed(
        client,
        sid,
        segments=[
            {
                "id": "s1",
                "speaker": "Speaker 1",
                "text": "we discussed the flamingo budget",
                "timestamp": 1,
                "edited": False,
            },
        ],
    )
    try:
        results = _search(client, "flamingo")
        assert sid in _ids(results)
        assert "flamingo" in next(r for r in results if r["id"] == sid)["snippet"]
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_ignores_tool_payloads_and_ui_only_roles():
    client = _client()
    sid = "search-noise"
    _seed(
        client,
        sid,
        chatHistory=[
            # Tool result payloads must not match: users never read these as messages.
            {
                "role": "assistant",
                "timestamp": "t",
                "content": [
                    {"type": "tool_result", "content": "grep found ocelot in config.py"},
                ],
            },
            # UI-only rows (cron_event) are invisible to search.
            {"role": "cron_event", "content": "cron says ocelot", "timestamp": "t"},
        ],
    )
    try:
        assert sid not in _ids(_search(client, "ocelot"))
        # JSON structure itself must not match either.
        assert sid not in _ids(_search(client, "tool_result"))
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_snippet_windows_long_messages():
    client = _client()
    sid = "search-snippet"
    long_msg = ("x" * 500) + " the hidden capybara clause " + ("y" * 500)
    _seed(
        client,
        sid,
        chatHistory=[
            {"role": "user", "content": long_msg, "timestamp": "t"},
        ],
    )
    try:
        hit = next(r for r in _search(client, "capybara") if r["id"] == sid)
        assert "capybara" in hit["snippet"]
        assert len(hit["snippet"]) < 150
        assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    finally:
        client.delete(f"/api/sessions/{sid}")


def test_empty_query_returns_nothing_and_route_not_shadowed():
    client = _client()
    assert _search(client, "") == []
    assert _search(client, "   ") == []
    # The literal /search path must not be captured by /{session_id}:
    # a shadowed route would return the 404 "not found" error envelope.
    r = client.get("/api/sessions/search", params={"q": ""})
    assert r.json() == {"results": [], "truncated": False}


def test_result_cap_reports_truncation():
    client = _client()
    sids = [f"search-cap-{i}" for i in range(3)]
    for sid in sids:
        _seed(
            client,
            sid,
            chatHistory=[
                {"role": "user", "content": "the axolotl migration plan", "timestamp": "t"},
            ],
        )
    try:
        r = client.get("/api/sessions/search", params={"q": "axolotl", "limit": 2})
        body = r.json()
        assert len(body["results"]) == 2
        assert body["truncated"] is True
        # A roomy limit finds everything and reports a complete scan.
        r = client.get("/api/sessions/search", params={"q": "axolotl", "limit": 200})
        body = r.json()
        assert {x["id"] for x in body["results"]} >= set(sids)
        assert body["truncated"] is False
    finally:
        for sid in sids:
            client.delete(f"/api/sessions/{sid}")
