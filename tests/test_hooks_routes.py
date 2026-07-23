"""WS-I HTTP API: v2 CRUD by id, dry-run Test, and project-trust approval."""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import plugin_hooks


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    saved = dict(plugin_hooks._hooks)
    plugin_hooks._hooks.clear()
    # Import after env is set so the router picks up the isolated data dir.
    from server.hooks.routes import router

    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), tmp_path
    plugin_hooks._hooks.clear()
    plugin_hooks._hooks.update(saved)


def test_crud_lifecycle(client):
    c, _ = client
    # Empty to start.
    r = c.get("/api/hooks")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2
    assert "Stop" in body["available_events"]
    assert all(len(v) == 0 for v in body["hooks"].values())

    # Add.
    r = c.post(
        "/api/hooks", json={"event": "PreToolUse", "matcher": "ws_write_file", "command": "exit 0"}
    )
    assert r.status_code == 200
    hook = r.json()["hook"]
    hid = hook["id"]
    assert hook["matcher"] == "ws_write_file"

    # It shows up under its event.
    assert any(h["id"] == hid for h in c.get("/api/hooks").json()["hooks"]["PreToolUse"])

    # Update.
    r = c.put(
        f"/api/hooks/{hid}",
        json={"event": "PreToolUse", "matcher": "*", "command": "exit 2", "on_error": "block"},
    )
    assert r.status_code == 200
    updated = r.json()["hook"]
    assert updated["matcher"] == "*" and updated["on_error"] == "block"

    # Delete.
    assert c.delete(f"/api/hooks/{hid}").status_code == 200
    assert len(c.get("/api/hooks").json()["hooks"]["PreToolUse"]) == 0


def test_update_missing_is_404(client):
    c, _ = client
    r = c.put("/api/hooks/nope", json={"event": "Stop", "command": "exit 0"})
    assert r.status_code == 404


def test_delete_missing_is_404(client):
    c, _ = client
    assert c.delete("/api/hooks/nope").status_code == 404


def test_invalid_event_rejected(client):
    c, _ = client
    assert c.post("/api/hooks", json={"event": "Nonsense", "command": "x"}).status_code == 400
    assert c.post("/api/hooks", json={"event": "Stop", "command": ""}).status_code == 400


def test_test_endpoint_reports_deny(client):
    c, _ = client
    r = c.post("/api/hooks/test", json={"command": "echo bad >&2; exit 2", "event": "PreToolUse"})
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 2
    assert body["decision"] == "deny"
    assert "bad" in body["reason"]
    # The synthetic payload is echoed back for the UI.
    assert body["payload"]["event"] == "PreToolUse"


def test_test_endpoint_reports_allow(client):
    c, _ = client
    body = c.post("/api/hooks/test", json={"command": "exit 0"}).json()
    assert body["decision"] == "allow"


def test_project_approve_flow(client, monkeypatch):
    c, tmp_path = client
    ws = tmp_path / "proj"
    (ws / ".whisper").mkdir(parents=True)
    (ws / ".whisper" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "exit 2", "matcher": "*"}]}})
    )
    monkeypatch.setattr("server.hooks.routes._workspace", lambda: str(ws))

    body = c.get("/api/hooks").json()
    assert body["project"]["status"] == "pending_approval"
    assert len(body["project"]["hooks"]["PreToolUse"]) == 1

    r = c.post("/api/hooks/project/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "trusted"
    body2 = c.get("/api/hooks").json()
    assert body2["project"]["status"] == "trusted"
    # Trusted project hooks are exposed so the panel can list/revoke them.
    assert len(body2["project"]["hooks"]["PreToolUse"]) == 1

    # Revoke returns them to inert.
    rr = c.post("/api/hooks/project/revoke", json={})
    assert rr.status_code == 200 and rr.json()["status"] == "pending_approval"
    assert c.get("/api/hooks").json()["project"]["status"] == "pending_approval"


def test_v1_legacy_file_is_read(client):
    c, tmp_path = client
    # A pre-existing v1 flat hooks.json must still load.
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "hooks.json").write_text(
        json.dumps({"hooks": [{"event": "PostToolUse", "tool": "*", "command": "echo hi"}]})
    )
    rows = c.get("/api/hooks").json()["hooks"]["PostToolUse"]
    assert len(rows) == 1 and rows[0]["command"] == "echo hi"
