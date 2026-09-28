"""Voice mode and Local mode: Local mode reports voice unavailable and refuses
the socket before a Sonic stream exists, and delegated voice work is refused,
never re-homed onto a cloud model the user did not pick."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.voice import routes as voice_routes
from server.voice import session as voice_session
from server.voice import tools as voice_tools
from server.voice.tools import ToolContext, run_tool


@pytest.fixture
def set_mode(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str) -> None:
        cfg = {**real, "model_mode": mode}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)

    return _set


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(voice_routes.router)
    return TestClient(app, base_url="http://localhost")


def _stub_ready(monkeypatch, looked_up: list[str]):
    """SDK present and credentials resolvable; record credential lookups."""
    monkeypatch.setattr(voice_routes, "sdk_availability", lambda: (True, None))

    def creds():
        looked_up.append("creds")
        return True

    monkeypatch.setattr(voice_routes, "_credentials_present", creds)


@pytest.mark.parametrize("mode", ["local", "hybrid", "cloud"])
def test_status_is_unavailable_exactly_in_local_mode(set_mode, monkeypatch, mode):
    set_mode(mode)
    looked_up: list[str] = []
    _stub_ready(monkeypatch, looked_up)
    body = _client().get("/api/voice/status").json()
    assert body["available"] is (mode != "local")
    if mode == "local":
        assert "Local mode" in body["reason"] and "Voice mode" in body["reason"]
        # The mode is the blocker: no AWS credential lookup happens for it.
        assert looked_up == []


def test_socket_refuses_before_opening_sonic_in_local_mode(set_mode, monkeypatch):
    set_mode("local")
    opened: list[str] = []

    async def opener(model_id, region):
        opened.append(model_id)
        raise AssertionError("no Sonic stream may open in Local mode")

    monkeypatch.setattr(voice_session, "open_stream", opener)
    with _client().websocket_connect("/ws/voice?session_id=s") as ws:
        ws.send_json({"type": "start", "history": [], "model_key": "opus5.0"})
        err = ws.receive_json()
        ended = ws.receive_json()
    assert err["type"] == "error" and "Local mode" in err["message"]
    assert ended == {"type": "ended", "reason": "unavailable"}
    assert opened == []


def _ctx(model_key: str | None, pending: dict | None = None) -> ToolContext:
    async def emit(ev):
        return None

    return ToolContext(
        session_id="s1",
        model_key=model_key,
        emit=emit,
        request_end=lambda: None,
        pending=pending if pending is not None else {},
    )


@pytest.fixture
def headless_calls(monkeypatch):
    calls: list[dict] = []

    async def fake(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        yield {"type": "text", "text": "done."}
        yield {"type": "done", "status": "completed", "session_id": "x"}

    monkeypatch.setattr("server.exec.headless.run_headless_turn", fake)
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k.startswith("local_"))
    return calls


def test_on_device_selection_is_refused_not_rehomed(set_mode, headless_calls):
    set_mode("hybrid")
    out = asyncio.run(run_tool("ask_assistant", {"request": "list files"}, _ctx("local_gemma")))
    assert out.startswith("Error:") and "cloud" in out
    assert headless_calls == []


def test_local_mode_refuses_delegated_work_for_any_selection(set_mode, headless_calls):
    set_mode("local")
    out = asyncio.run(run_tool("ask_assistant", {"request": "list files"}, _ctx("opus5.0")))
    assert out.startswith("Error:") and "Local mode" in out
    assert headless_calls == []


def test_cloud_selection_runs_on_exactly_that_model(set_mode, headless_calls):
    set_mode("hybrid")
    out = asyncio.run(run_tool("ask_assistant", {"request": "list files"}, _ctx("opus5.0")))
    assert out == "done."
    assert [c["model_key"] for c in headless_calls] == ["opus5.0"]


def test_resolve_refuses_before_running_the_approval_in_local_mode(
    set_mode, headless_calls, monkeypatch
):
    set_mode("local")
    executed: list[str] = []

    class _Spec:
        category = "cli"

        async def executor(self, payload):
            executed.append(payload["command"])
            raise AssertionError("the approval must not run")

    monkeypatch.setattr("server.approval.registry.get", lambda action: _Spec())
    pending = {
        "type": "approval_request",
        "tool_use_id": "tu-1",
        "action": "command",
        "payload": {"command": "rm -rf build"},
        "run_id": "voice-s1-abc",
    }
    ctx = _ctx("opus5.0", pending)
    out = asyncio.run(voice_tools._resolve_request("yes", ctx))
    assert out.startswith("Error:") and "Local mode" in out
    assert executed == [] and headless_calls == []
    assert ctx.pending == pending  # nothing was claimed


def _live_session(streams: list, events: list):
    from tests.test_voice_session import FakeStream

    async def opener(model_id, region):
        s = FakeStream()
        streams.append(s)
        return s

    async def emit(ev):
        events.append(ev)

    return voice_session.VoiceSession(
        session_id="s1",
        config=voice_session.VoiceConfig(voice_id="matthew", system_prompt="SYS"),
        emit=emit,
        opener=opener,
    )


def test_switch_to_local_mode_ends_a_live_conversation(set_mode):
    """A conversation opened in Hybrid stops streaming to Sonic once the app is
    in Local mode: the stream closes within a renew-loop tick, a delegated run
    still going is cancelled, and the browser is told why."""
    set_mode("hybrid")
    streams: list = []
    events: list = []

    async def scenario():
        vs = _live_session(streams, events)
        await vs.start()
        run = asyncio.create_task(asyncio.sleep(60))
        vs._run_tasks.add(run)
        set_mode("local")
        await asyncio.wait_for(vs.done.wait(), timeout=5)
        return run

    run = asyncio.run(scenario())
    assert len(streams) == 1 and streams[0].closed
    assert run.cancelled()
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "Local mode" in errors[-1]["message"]
    assert events[-1] == {"type": "ended", "reason": "unavailable"}


def test_renewal_after_a_switch_to_local_mode_opens_no_stream(set_mode):
    set_mode("cloud")
    streams: list = []
    events: list = []

    async def scenario():
        vs = _live_session(streams, events)
        await vs.start()
        set_mode("local")
        await vs._reopen(vs._live, why="8-minute renewal")
        return vs

    vs = asyncio.run(scenario())
    assert len(streams) == 1 and streams[0].closed
    assert vs.done.is_set() and events[-1] == {"type": "ended", "reason": "unavailable"}
    assert "renewing" not in [e["type"] for e in events]
