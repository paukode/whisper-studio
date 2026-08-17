"""Session-scoped workflow approval grants.

Approving a preview card (POST /api/workflows/runs) grants that exact script,
by hash, for that session: re-running it — by saved name or as the same inline
script — launches with no second card until the app restarts or the session is
deleted. The Settings trust tick stays the only durable trust; any edit to the
script changes the hash and brings the card back.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)
pytestmark = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")

_NOOP = "export const meta = { name: 'noop', description: 'no agents', phases: ['a'] }\nphase('a')\nreturn { ok: 1 }\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    from server.security import permissions as P

    monkeypatch.setattr(P, "PERMISSIONS_PATH", str(tmp_path / "permissions.json"))
    from server.tasks import registry

    monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import launch_policy, manager

    manager._live.clear()
    manager._task_ids.clear()
    launch_policy._session_grants.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()
    launch_policy._session_grants.clear()


def _client():
    from server.workflows.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _run_tool(tool_input, session_id):
    from server.workflows.tools import execute_workflow_run

    return asyncio.run(execute_workflow_run(tool_input, session_id, "", None))


# ── the registry itself ──────────────────────────────────────────────────────


def test_grant_is_per_session_and_droppable():
    from server.workflows.launch_policy import (
        drop_session_grants,
        grant_session,
        is_session_granted,
    )

    grant_session("s1", "h1")
    assert is_session_granted("s1", "h1")
    assert not is_session_granted("s2", "h1")
    assert not is_session_granted("s1", "h2")
    # Empty ids never grant or match: an anonymous caller cannot inherit one.
    grant_session("", "h3")
    assert not is_session_granted("", "h3")
    drop_session_grants("s1")
    assert not is_session_granted("s1", "h1")


def test_session_delete_drops_grants(tmp_path):
    from server.infrastructure import sessions
    from server.workflows.launch_policy import grant_session, is_session_granted

    # The fixture repoints DB_PATH after import, so the schema _ensure_db built
    # at import time lives elsewhere — build it at the patched path first.
    sessions._ensure_db()
    grant_session("doomed", "h1")
    sessions._delete_session_sync("doomed")
    assert not is_session_granted("doomed", "h1")


# ── approve once, run silently for the rest of the session ──────────────────


def test_approving_card_grants_the_inline_script_for_the_session():
    # First proposal: default mode shows the card.
    out, side = _run_tool({"script": _NOOP}, "s1")
    assert side and "workflow_preview" in side[0]

    # The user clicks Approve — the card POSTs the script to the launch route.
    r = _client().post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    assert r.status_code == 200

    # The same script re-proposed in the same session launches, no card.
    out, side = _run_tool({"script": _NOOP}, "s1")
    body = json.loads(out)
    assert body["session_approved"] is True and body["run_id"]
    assert side and "workflow_started" in side[0]

    # Another session still gets the card.
    out, side = _run_tool({"script": _NOOP}, "s2")
    assert side and "workflow_preview" in side[0]


def test_grant_covers_the_saved_name_with_the_same_script():
    from server.workflows.tools import execute_workflow_save

    asyncio.run(execute_workflow_save({"name": "my-flow", "script": _NOOP}))

    # Untrusted saved name: card first.
    out, side = _run_tool({"name": "my-flow"}, "s1")
    assert side and "workflow_preview" in side[0]

    # Approve from the card (same bytes).
    r = _client().post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    assert r.status_code == 200

    # Run by name now launches silently — the grant is keyed by script hash.
    out, side = _run_tool({"name": "my-flow"}, "s1")
    body = json.loads(out)
    assert body["session_approved"] is True
    assert side and "workflow_started" in side[0]


def test_editing_the_script_brings_the_card_back():
    _client().post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})

    edited = _NOOP.replace("{ ok: 1 }", "{ ok: 2 }")
    out, side = _run_tool({"script": edited}, "s1")
    assert side and "workflow_preview" in side[0]


def test_grant_beats_dontask_like_other_session_approvals(monkeypatch):
    # A category mode of dontAsk declines NEW scripts, but a script the user
    # already approved this session launches — same precedence as the
    # category-approval system, where a session "allow" beats dontAsk.
    from server.security import permissions as P

    _client().post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    monkeypatch.setattr(
        P,
        "load_permissions",
        lambda: {"mode": "default", "rules": [], "category_modes": {"workflow": "dontAsk"}},
    )
    out, side = _run_tool({"script": _NOOP}, "s1")
    assert json.loads(out)["session_approved"] is True

    # A different (unapproved) script is still declined.
    other = _NOOP.replace("noop", "other")
    out, side = _run_tool({"script": other}, "s1")
    assert "not started" in out.lower() and side == []


# ── the /workflow slash-command route honors grants too ─────────────────────


def test_launch_by_name_route_honors_a_session_grant():
    from server.workflows import store

    store.save_script("wf", _NOOP, {"description": "d", "phases": ["a"]})
    c = _client()

    # Untrusted, no grant: 403 with preview_required.
    r = c.post("/api/workflows/runs", json={"name": "wf", "session_id": "s1"})
    assert r.status_code == 403 and r.json()["preview_required"] is True

    # Approve the script once (the card's POST), then by-name launches.
    assert (
        c.post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"}).status_code == 200
    )
    r2 = c.post("/api/workflows/runs", json={"name": "wf", "session_id": "s1"})
    assert r2.status_code == 200 and r2.json()["run_id"]

    # A different session is still gated.
    r3 = c.post("/api/workflows/runs", json={"name": "wf", "session_id": "s2"})
    assert r3.status_code == 403
