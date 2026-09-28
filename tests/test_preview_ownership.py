"""A preview server belongs to the chat session that started it.

Field report: one chat was building an app on a preview while another chat
debugged with a preview; stopping the debugging chat's work stopped the app.
Previews were one global, ownerless map keyed by a model-chosen name, the
preview_start error told a clashing chat to "stop it first", and preview_stop
from any chat killed any server. Now another chat may list and inspect a
preview but cannot stop, replace or drive it, and the refusal names the owner.

The tests drive the real dispatch: the preview router builds the approval
sentinel, the registered ApprovalSpec shapes the payload exactly as the
approval card and the inline auto-approve path receive it, and the approval
executor runs it.
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.preview import manager as m

CHAT_A = "chat-a-0001"
CHAT_B = "chat-b-0002"


@pytest.fixture
def previews(monkeypatch):
    fresh = m.PreviewManager()
    monkeypatch.setattr(m, "preview_manager", fresh)
    monkeypatch.setattr("server.preview.routes.preview_manager", fresh)
    monkeypatch.setattr(
        "server.infrastructure.sessions.get_session_meta",
        lambda sid: {"title": "Build the app"} if sid == CHAT_A else None,
    )
    from server.approval.bootstrap import register_defaults

    register_defaults()
    return fresh


def _run(coro):
    return asyncio.run(coro)


def _approved(tool: str, chat: str, **tool_input) -> dict:
    """The payload an approval executor receives for this chat's tool call."""
    from server.approval import registry as approval_registry
    from server.preview.router import execute_preview_tool

    out = _run(execute_preview_tool(tool, {**tool_input, "__session_id__": chat}))
    assert out.startswith("[WS_APPROVAL]"), out
    parsed = json.loads(out[len("[WS_APPROVAL]") :])
    return approval_registry.get(parsed["action"]).build_payload(parsed)


def _execute(tool: str, chat: str, **tool_input):
    from server.approval import registry as approval_registry

    payload = _approved(tool, chat, **tool_input)
    return _run(approval_registry.get(tool).executor(payload))


def _start_browser_only(previews, name: str, owner: str) -> None:
    """A registered preview with no dev-server process (nothing to spawn)."""
    _run(previews.start_session(name, command=None, cwd="/tmp", owner=owner))


def test_the_calling_chat_rides_in_the_payload_and_cannot_be_spoofed(previews):
    payload = _approved("preview_stop", CHAT_B, session_name="dev", session_id=CHAT_A)
    assert payload["session_id"] == CHAT_B


def test_another_chat_cannot_stop_a_preview_and_is_told_whose_it_is(previews):
    _start_browser_only(previews, "dev", CHAT_A)

    outcome = _execute("preview_stop", CHAT_B, session_name="dev")

    assert not outcome.ok
    assert "Build the app" in outcome.error, "the refusal must name the owning chat"
    assert previews.get("dev") is not None, "chat B stopped chat A's preview"

    assert _execute("preview_stop", CHAT_A, session_name="dev").ok
    assert previews.get("dev") is None


def test_a_name_clash_across_chats_never_steers_toward_stopping_it(previews):
    _start_browser_only(previews, "dev", CHAT_A)

    theirs = _run(
        m.start_preview_session(
            {
                "session_name": "dev",
                "runtimeExecutable": "true",
                "cwd": "/tmp",
                "session_id": CHAT_B,
            }
        )
    )
    assert theirs[0] is False
    assert "Build the app" in theirs[1]
    assert "stop it first" not in theirs[1]
    assert "different session_name" in theirs[1]

    mine = _run(
        m.start_preview_session(
            {
                "session_name": "dev",
                "runtimeExecutable": "true",
                "cwd": "/tmp",
                "session_id": CHAT_A,
            }
        )
    )
    assert mine[0] is False
    assert "already running in this chat" in mine[1]
    assert previews.get("dev").owner == CHAT_A


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("preview_navigate", {"url": "http://localhost:5173"}),
        ("preview_click", {"selector": "#go"}),
        ("preview_fill", {"selector": "#q", "value": "x"}),
        ("preview_eval", {"expression": "1+1"}),
        ("preview_resize", {"preset": "mobile"}),
    ],
)
def test_another_chat_cannot_drive_a_preview(previews, tool, extra):
    _start_browser_only(previews, "dev", CHAT_A)
    outcome = _execute(tool, CHAT_B, session_name="dev", **extra)
    assert not outcome.ok
    assert "belongs to" in outcome.error and "Build the app" in outcome.error
    assert previews.get("dev").browser.page is None, "the other chat's call reached the browser"


def test_preview_list_separates_this_chat_from_other_chats(previews):
    from server.preview.router import execute_preview_tool

    _start_browser_only(previews, "dev", CHAT_A)
    _start_browser_only(previews, "debug", CHAT_B)

    seen_by_b = json.loads(_run(execute_preview_tool("preview_list", {"__session_id__": CHAT_B})))
    assert [r["id"] for r in seen_by_b["this_chat"]] == ["debug"]
    assert [r["id"] for r in seen_by_b["other_chats"]] == ["dev"]

    seen_by_a = json.loads(_run(execute_preview_tool("preview_list", {"__session_id__": CHAT_A})))
    assert [r["id"] for r in seen_by_a["this_chat"]] == ["dev"]


def _client() -> TestClient:
    from server.preview.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_live_pane_stop_only_reaches_its_own_chats_preview(previews):
    _start_browser_only(previews, "dev", CHAT_A)
    c = _client()

    rows = c.get("/api/preview/sessions").json()["sessions"]
    assert rows[0]["owner"] == CHAT_A

    r = c.delete("/api/preview/sessions/dev", params={"session_id": CHAT_B})
    assert r.status_code == 409
    assert "Build the app" in r.json()["error"]
    assert previews.get("dev") is not None

    r = c.delete("/api/preview/sessions/dev", params={"session_id": CHAT_A})
    assert r.status_code == 200
    assert previews.get("dev") is None


def test_settings_kill_switch_still_stops_any_preview(previews):
    _start_browser_only(previews, "dev", CHAT_A)
    r = _client().delete("/api/preview/sessions/dev")
    assert r.status_code == 200
    assert previews.get("dev") is None


def test_live_pane_restart_is_scoped_to_its_chat(previews):
    _start_browser_only(previews, "dev", CHAT_A)
    c = _client()
    assert c.post("/api/preview/sessions", json={"name": "dev"}).status_code == 400
    r = c.post("/api/preview/sessions", json={"name": "dev", "session_id": CHAT_B})
    assert r.status_code == 409
    r = c.post("/api/preview/sessions", json={"name": "dev", "session_id": CHAT_A})
    assert r.json() == {"started": True, "name": "dev", "reused": True}


# ── Restart of a preview whose dev server exited ─────────────────────────────


class _Proc:
    def __init__(self, alive: bool):
        self.alive = alive
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _restart_setup(monkeypatch, previews, *, alive: bool, owner: str):
    spawned: list = []

    async def _spawn(command, cwd, env=None):
        spawned.append(command)
        return _Proc(True)

    monkeypatch.setattr(m.DevServerProcess, "spawn", staticmethod(_spawn))
    monkeypatch.setattr(
        "server.preview.launch_config.resolve_launch_command",
        lambda name: {"command": ["npm", "run", "dev"], "port": 5173},
    )
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: "/tmp")
    old = _Proc(alive)
    previews._sessions["web"] = m.PreviewSession(id="web", process=old, port=5173, owner=owner)
    return spawned, old


def test_restart_brings_back_the_chats_own_crashed_preview(previews, monkeypatch):
    from server.preview import routes

    spawned, old = _restart_setup(monkeypatch, previews, alive=False, owner=CHAT_A)
    reply = _run(routes.start_session(_Req({"name": "web", "session_id": CHAT_A})))
    assert reply == {"started": True, "name": "web"}
    assert spawned == [["npm", "run", "dev"]] and old.stopped
    assert previews.get("web").process.alive and previews.get("web").owner == CHAT_A


def test_restart_reuses_a_preview_that_is_still_running(previews, monkeypatch):
    from server.preview import routes

    spawned, old = _restart_setup(monkeypatch, previews, alive=True, owner=CHAT_A)
    reply = _run(routes.start_session(_Req({"name": "web", "session_id": CHAT_A})))
    assert reply["reused"] is True and spawned == [] and previews.get("web").process is old


def test_another_chat_cannot_restart_a_crashed_preview(previews, monkeypatch):
    from server.preview import routes

    spawned, _old = _restart_setup(monkeypatch, previews, alive=False, owner=CHAT_A)
    reply = _run(routes.start_session(_Req({"name": "web", "session_id": CHAT_B})))
    assert reply.status_code == 409 and spawned == []


def test_the_model_can_start_its_crashed_preview_again(previews, monkeypatch):
    """preview_start for the same name used to be refused as 'already running
    in this chat' while the pane said the server had stopped."""
    spawned, _old = _restart_setup(monkeypatch, previews, alive=False, owner=CHAT_A)
    ok, msg = _run(m.start_preview_session({"session_name": "web", "session_id": CHAT_A}))
    assert ok, msg
    assert spawned == [["npm", "run", "dev"]]
