"""WS-I HTTP API: v2 CRUD by id, dry-run Test, and project-trust approval."""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.hooks import config_loader as hooks_cfg
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
    monkeypatch.setattr("server.hooks.routes._current_workspace", lambda: str(ws))

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


@pytest.fixture
def notices(monkeypatch):
    import server.notifications as N

    seen = []
    monkeypatch.setattr(N, "old_format_notice", lambda **kw: seen.append(kw))
    return seen


def test_a_v1_file_is_not_read_or_overwritten(client, notices):
    c, tmp_path = client
    hooks_file = tmp_path / "data" / "hooks.json"
    hooks_file.write_text(
        json.dumps({"hooks": [{"event": "PostToolUse", "tool": "*", "command": "echo hi"}]})
    )
    before = hooks_file.read_bytes()

    assert c.get("/api/hooks").json()["hooks"]["PostToolUse"] == []
    assert notices and {n["title"] for n in notices} == {hooks_cfg.V1_NOTICE_TITLE}
    # Saving a hook would replace the file, so it is refused.
    r = c.post("/api/hooks", json={"event": "Stop", "command": "exit 0"})
    assert r.status_code == 409
    assert "old v1 format" in r.json()["error"]
    assert hooks_file.read_bytes() == before


def test_an_invalid_on_error_or_regex_matcher_is_refused(client):
    c, _ = client
    r = c.post("/api/hooks", json={"event": "PreToolUse", "command": "exit 2", "on_error": "Block"})
    assert r.status_code == 400 and "on_error" in r.json()["error"]
    r = c.post("/api/hooks", json={"event": "PreToolUse", "command": "exit 2", "matcher": "/[/"})
    assert r.status_code == 400 and "regular expression" in r.json()["error"]
    assert all(len(v) == 0 for v in c.get("/api/hooks").json()["hooks"].values())


def test_an_unreadable_file_is_not_overwritten(client):
    c, tmp_path = client
    hooks_file = tmp_path / "data" / "hooks.json"
    # A trailing comma: the guard in it cannot be read, so it is not run.
    hooks_file.write_text(
        '{"version": 2, "hooks": {"PreToolUse": [{"command": "exit 2", "on_error": "block"},]}}'
    )
    before = hooks_file.read_bytes()

    assert c.get("/api/hooks").json()["hooks"]["PreToolUse"] == []
    # Saving would replace the file and drop the guard, so it is refused.
    r = c.post("/api/hooks", json={"event": "Stop", "command": "exit 0"})
    assert r.status_code == 409
    assert "cannot be read" in r.json()["error"]
    assert hooks_file.read_bytes() == before


def test_turn_end_hooks_are_not_run_and_are_kept_on_save(client, notices):
    c, tmp_path = client
    hooks_file = tmp_path / "data" / "hooks.json"
    turn_end = [{"command": "echo done", "matcher": "*"}]
    hooks_file.write_text(json.dumps({"version": 2, "hooks": {"TurnEnd": turn_end}}))

    body = c.get("/api/hooks").json()
    assert all(len(v) == 0 for v in body["hooks"].values())
    assert {n["title"] for n in notices} == {hooks_cfg.TURN_END_NOTICE_TITLE}
    # TurnEnd is no longer an event name the API takes.
    assert c.post("/api/hooks", json={"event": "TurnEnd", "command": "x"}).status_code == 400

    r = c.post("/api/hooks", json={"event": "Stop", "command": "exit 0"})
    assert r.status_code == 200
    on_disk = json.loads(hooks_file.read_text())
    assert on_disk["hooks"]["TurnEnd"] == turn_end  # left as the user wrote it
    assert len(on_disk["hooks"]["Stop"]) == 1


def test_an_old_tool_key_hook_is_not_run_and_is_kept_on_save(client, notices):
    c, tmp_path = client
    hooks_file = tmp_path / "data" / "hooks.json"
    old = {"tool": "Bash", "command": "exit 2", "on_error": "block"}
    hooks_file.write_text(json.dumps({"version": 2, "hooks": {"PreToolUse": [old]}}))

    # Not widened to "*": the guard is not listed and not run, and a notice says so.
    assert c.get("/api/hooks").json()["hooks"]["PreToolUse"] == []
    assert hooks_cfg.merged_for_event("PreToolUse", None) == []
    assert {n["title"] for n in notices} == {hooks_cfg.TOOL_KEY_NOTICE_TITLE}

    # Saving another hook keeps the old entry as the user wrote it.
    r = c.post("/api/hooks", json={"event": "PreToolUse", "matcher": "x", "command": "exit 0"})
    assert r.status_code == 200
    on_disk = json.loads(hooks_file.read_text())["hooks"]["PreToolUse"]
    assert old in on_disk and len(on_disk) == 2


def test_a_body_with_the_old_tool_key_is_refused(client):
    c, _ = client
    r = c.post("/api/hooks", json={"event": "PreToolUse", "tool": "Bash", "command": "exit 2"})
    assert r.status_code == 400 and '"tool"' in r.json()["error"]
    assert all(len(v) == 0 for v in c.get("/api/hooks").json()["hooks"].values())


def test_test_endpoint_does_not_run_a_command_with_an_old_variable(client):
    c, _ = client
    body = c.post("/api/hooks/test", json={"command": 'test "$WHISPER_TOOL" = x'}).json()
    assert body["exit_code"] == -1
    assert body["decision"] == "error"
    assert "WHISPER_TOOL" in body["reason"] and "tool_name" in body["reason"]


def test_test_endpoint_rejects_an_unknown_event(client):
    c, _ = client
    r = c.post("/api/hooks/test", json={"command": "exit 0", "event": "TurnEnd"})
    assert r.status_code == 400
