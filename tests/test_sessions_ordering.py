"""Sessions list must respect updatedAt for ordering AND echo it back to the
client as `date` so the sidebar can render relative timestamps.

Earlier the PUT/beacon handlers only read `body.get("date", "")`, but the
React client sends `updatedAt`. That left `updated_at` empty, broke the
ORDER BY updated_at DESC sort, and made formatSessionTime("") render blank.
"""

# Build a thin app with just the sessions router so we don't need the full
# main.py lifespan (which spawns MCP servers and downloads ML models).
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    return TestClient(app)


def test_put_session_persists_updated_at_and_orders_by_recency():
    client = _client()

    # Create three sessions with explicit, deliberately out-of-order timestamps.
    payloads = [
        ("aaa", "First", "2026-01-01T00:00:00Z"),
        ("bbb", "Second", "2026-03-01T00:00:00Z"),
        ("ccc", "Third", "2026-02-01T00:00:00Z"),
    ]
    for sid, title, ts in payloads:
        r = client.put(
            f"/api/sessions/{sid}",
            json={
                "id": sid,
                "title": title,
                "createdAt": ts,
                "updatedAt": ts,
                "segments": [],
                "chatHistory": [],
                "speakerNames": {},
            },
        )
        assert r.status_code == 200, r.text

    listing = client.get("/api/sessions").json()
    # The summary endpoint exposes updated_at as `date` for the React client.
    by_id = {row["id"]: row for row in listing if row["id"] in {sid for sid, _, _ in payloads}}
    assert by_id["bbb"]["date"] == "2026-03-01T00:00:00Z"
    assert by_id["ccc"]["date"] == "2026-02-01T00:00:00Z"
    assert by_id["aaa"]["date"] == "2026-01-01T00:00:00Z"

    # Order in the listing must be newest-first.
    seen = [row["id"] for row in listing if row["id"] in by_id]
    assert seen == ["bbb", "ccc", "aaa"], f"unexpected order: {seen}"

    # Cleanup: drop our test rows so we don't pollute the dev DB.
    for sid, _, _ in payloads:
        client.delete(f"/api/sessions/{sid}")
