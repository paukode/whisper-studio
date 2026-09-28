"""The MCP registry keeps one live server list, whoever changes it.

Contracts pinned here:

1. reconcile() is the one place servers start and stop: removed or disabled
   servers stop, new ones start, a launch-settings change restarts, and an
   approval-only edit applies without a restart.
2. A server that failed is not respawned on every pass; a Restart or a
   launch-settings change retries it.
3. An unreadable mcp_servers.json leaves the running servers alone and is
   reported, until the file is fixed.
4. run() applies edits made outside the routes (the assistant, an editor)
   without a restart.
5. Every change bumps the revision and publishes one app-wide
   `mcp_changed` event.
6. A server that announces a changed tool list is re-listed, end to end
   against a real stdio MCP server.
"""

import asyncio
import json
import os
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server import mcp_routes
from server.agents.event_bus import event_bus
from server.mcp import MCPManager, _list_all


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", str(path))
    return path


def _write(path, servers: dict) -> None:
    path.write_text(json.dumps({"servers": servers}))


def _entry(command: str = "c", **extra) -> dict:
    return {"command": command, "args": [], "env": {}, "enabled": True, **extra}


def _recording_manager(monkeypatch, fail: set[str] | None = None):
    """A manager whose start/stop only record, so no subprocess is spawned.
    Names in ``fail`` come up in the error state, like a bad command."""
    mgr = MCPManager()
    calls: list[tuple[str, str]] = []

    async def _start(name, config):
        calls.append(("start", name))
        if fail and name in fail:
            mgr._sessions[name] = {
                "status": "error",
                "error": "boom",
                "config": config,
                "tools": {},
            }
            return
        mgr._sessions[name] = {"status": "connected", "config": config, "tools": {}}

    async def _stop(name):
        calls.append(("stop", name))
        mgr._sessions.pop(name, None)

    monkeypatch.setattr(mgr, "start_server", _start)
    monkeypatch.setattr(mgr, "stop_server", _stop)
    return mgr, calls


# --- (1) reconcile ---------------------------------------------------------


def test_reconcile_starts_stops_and_restarts_to_match_the_file(config_path, monkeypatch):
    mgr, calls = _recording_manager(monkeypatch)
    _write(config_path, {"alpha": _entry(), "beta": _entry(), "gamma": _entry()})
    asyncio.run(mgr.reconcile())
    assert sorted(calls) == [("start", "alpha"), ("start", "beta"), ("start", "gamma")]

    calls.clear()
    _write(
        config_path,
        {
            "alpha": _entry(),  # unchanged: left running
            "beta": _entry(command="new-binary"),  # launch change: restarted
            "gamma": {**_entry(), "enabled": False},  # disabled: stopped
            "delta": _entry(),  # new: started
        },
    )
    asyncio.run(mgr.reconcile())
    assert sorted(calls) == [
        ("start", "beta"),
        ("start", "delta"),
        ("stop", "beta"),
        ("stop", "gamma"),
    ]
    assert set(mgr._sessions) == {"alpha", "beta", "delta"}


def test_approval_only_edit_applies_without_a_restart(config_path, monkeypatch):
    mgr, calls = _recording_manager(monkeypatch)
    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())
    calls.clear()
    before = mgr.revision

    _write(config_path, {"alpha": _entry(approval_mode="approve", disabled_tools=["rm"])})
    asyncio.run(mgr.reconcile())

    assert calls == []
    assert mgr._sessions["alpha"]["config"]["approval_mode"] == "approve"
    # The edit is still a visible change for every window.
    assert mgr.revision > before


def test_a_pass_with_nothing_new_changes_nothing(config_path, monkeypatch):
    mgr, calls = _recording_manager(monkeypatch)
    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())
    calls.clear()
    before = mgr.revision

    asyncio.run(mgr.reconcile())

    assert calls == []
    assert mgr.revision == before


# --- (2) failed servers ----------------------------------------------------


def test_failed_server_is_retried_only_by_restart_or_a_settings_change(config_path, monkeypatch):
    mgr, calls = _recording_manager(monkeypatch, fail={"alpha"})
    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())
    assert mgr.get_status()["alpha"]["status"] == "error"

    calls.clear()
    asyncio.run(mgr.reconcile())
    assert calls == []  # not respawned on every pass

    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)
    asyncio.run(mcp_routes.mcp_restart_server("alpha"))
    assert ("start", "alpha") in calls

    calls.clear()
    _write(config_path, {"alpha": _entry(command="fixed")})
    asyncio.run(mgr.reconcile())
    assert calls == [("stop", "alpha"), ("start", "alpha")]


# --- (3) unreadable config --------------------------------------------------


def test_unreadable_file_keeps_servers_running_and_is_reported(config_path, monkeypatch):
    mgr, calls = _recording_manager(monkeypatch)
    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)
    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())
    calls.clear()

    config_path.write_text('{"servers": {"alpha": ')  # a half-typed hand edit
    asyncio.run(mgr.reconcile())
    assert calls == []
    assert "alpha" in mgr._sessions
    assert mgr.config_error and "not a valid server list" in mgr.config_error
    assert asyncio.run(mcp_routes.mcp_servers_status())["config_error"] == mgr.config_error

    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())
    assert mgr.config_error is None
    assert calls == []


def test_changes_are_refused_while_the_file_is_unreadable(config_path, monkeypatch):
    """Saving over a broken hand edit would drop every server it holds."""
    mgr, _calls = _recording_manager(monkeypatch)
    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)
    broken = '{"servers": {"alpha": {"command": "a"}, "beta": '
    config_path.write_text(broken)

    class _Req:
        async def json(self):
            return {"name": "gamma", "command": "g"}

    for call in (
        mcp_routes.mcp_add_server(_Req()),
        mcp_routes.mcp_patch_server("alpha", _Req()),
        mcp_routes.mcp_update_server("alpha", _Req()),
        mcp_routes.mcp_remove_server("alpha"),
    ):
        assert asyncio.run(call).status_code == 409
    assert config_path.read_text() == broken


def test_status_route_reports_the_revision_and_config_error(config_path, monkeypatch):
    mgr, _calls = _recording_manager(monkeypatch)
    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)
    _write(config_path, {"alpha": _entry()})
    asyncio.run(mgr.reconcile())

    body = asyncio.run(mcp_routes.mcp_servers_status())
    assert body["revision"] == mgr.revision
    assert body["config_error"] is None
    assert body["servers"]["alpha"]["status"] == "connected"


def test_status_route_coerces_hand_edited_fields(config_path, monkeypatch):
    """The file is edited by hand and by the assistant; odd values must not
    break the one list every window reads."""
    mgr, _calls = _recording_manager(monkeypatch)
    monkeypatch.setattr(mcp_routes, "mcp_manager", mgr)
    _write(
        config_path,
        {
            "odd": {
                "command": "srv",
                "args": ["--port", 8080],
                "env": {"N": 1},
                "enabled_tools": "a,b",
            },
            "not-an-entry": "oops",
        },
    )
    body = asyncio.run(mcp_routes.mcp_servers_status())
    assert set(body["servers"]) == {"odd"}
    odd = body["servers"]["odd"]
    assert odd["args"] == ["--port", "8080"]
    assert odd["env"] == {"N": "1"}
    assert odd["enabled_tools"] == []


# --- (4) the watcher ---------------------------------------------------------


def test_run_applies_an_edit_made_outside_the_routes(config_path, monkeypatch):
    """The assistant or an editor writes the file directly; the server comes
    up with no restart and no route call."""
    mgr, calls = _recording_manager(monkeypatch)
    monkeypatch.setattr(mcp_module, "CONFIG_POLL_S", 0.01)

    async def scenario():
        runner = asyncio.create_task(mgr.run())
        try:
            await asyncio.sleep(0.05)
            assert calls == []
            _write(config_path, {"added-live": _entry()})
            for _ in range(200):
                if ("start", "added-live") in calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
    assert ("start", "added-live") in calls


def test_save_config_is_atomic_and_leaves_no_temp_files(config_path):
    mgr = MCPManager()
    mgr.save_config({"alpha": _entry()})
    assert json.loads(config_path.read_text())["servers"]["alpha"]["command"] == "c"
    assert os.listdir(config_path.parent) == ["mcp_servers.json"]


# --- (5) the change event ----------------------------------------------------


def test_every_change_publishes_one_app_wide_event(config_path, monkeypatch):
    mgr, _calls = _recording_manager(monkeypatch)
    _write(config_path, {"alpha": _entry()})

    async def scenario():
        queue = event_bus.subscribe_all()
        try:
            await mgr.reconcile()
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return events
        finally:
            event_bus.unsubscribe_all(queue)

    events = asyncio.run(scenario())
    changes = [ev for sid, ev in events if ev.get("type") == "mcp_changed"]
    assert changes, "reconcile published no mcp_changed event"
    # App-wide (no session id) and carrying the revision the list now has.
    assert all(sid == "" for sid, ev in events if ev.get("type") == "mcp_changed")
    assert changes[-1]["revision"] == mgr.revision


# --- (6) tool list changes -------------------------------------------------


def test_list_all_follows_cursors_and_stops_on_a_repeated_one():
    pages = {
        None: SimpleNamespace(tools=["a", "b"], nextCursor="p2"),
        "p2": SimpleNamespace(tools=["c"], nextCursor="p3"),
        "p3": SimpleNamespace(tools=["d"], nextCursor=None),
    }

    async def fetch(params=None):
        return pages[params.cursor if params else None]

    assert asyncio.run(_list_all(fetch, "tools")) == ["a", "b", "c", "d"]

    async def looping(params=None):
        return SimpleNamespace(tools=["x"], nextCursor="same")

    assert asyncio.run(_list_all(looping, "tools")) == ["x", "x"]


_GROWING_SERVER = textwrap.dedent(
    """
    from mcp.server.fastmcp import Context, FastMCP

    server = FastMCP("growing")

    @server.tool()
    async def grow(ctx: Context) -> str:
        def late_tool() -> str:
            return "late"

        server.add_tool(late_tool, name="late_tool")
        await ctx.session.send_tool_list_changed()
        return "grown"

    server.run()
    """
)


def test_a_tool_list_change_is_relisted_live(tmp_path, config_path, monkeypatch):
    """A real stdio server adds a tool after startup and says so; the new tool
    reaches the registry (and so the model and every window) with no
    restart. This is what a server that registers its tools late needs."""
    script = tmp_path / "growing_server.py"
    script.write_text(_GROWING_SERVER)
    _write(config_path, {"growing": _entry(command=sys.executable, args=[str(script)])})
    mgr = MCPManager()

    async def scenario():
        await mgr.reconcile()
        try:
            assert mgr.get_status()["growing"]["status"] == "connected", mgr.get_status()
            assert mgr.get_status()["growing"]["tools"] == ["mcp__growing__grow"]
            before = mgr.revision
            assert await mgr.call_tool("mcp__growing__grow", {}) == "grown"
            for _ in range(500):
                if "mcp__growing__late_tool" in mgr.get_status()["growing"]["tools"]:
                    break
                await asyncio.sleep(0.01)
            advertised = {t["name"] for t in mgr.get_bedrock_tools()}
            return mgr.get_status()["growing"]["tools"], advertised, mgr.revision > before
        finally:
            await mgr.stop_all()

    tools, advertised, bumped = asyncio.run(scenario())
    assert sorted(tools) == ["mcp__growing__grow", "mcp__growing__late_tool"]
    assert "mcp__growing__late_tool" in advertised  # the model sees it next round
    assert bumped


def test_connection_that_ends_is_reported_not_dropped(config_path, monkeypatch):
    """A connection that dies after startup shows as an error under its On
    switch instead of silently disappearing from the list."""
    mgr = MCPManager()
    stop_event = asyncio.Event()
    mgr._sessions["alpha"] = {
        "status": "connected",
        "config": _entry(),
        "tools": {"mcp__alpha__ping": MagicMock()},
        "stop_event": stop_event,
    }
    mgr._tools["mcp__alpha__ping"] = {"server_name": "alpha"}

    class _Dies:
        async def __aenter__(self):
            raise ConnectionResetError("pipe closed")

        async def __aexit__(self, *exc):
            return False

    async def scenario():
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        ready.set_result(True)  # already published, as after a real start
        import mcp.client.stdio

        monkeypatch.setattr(mcp.client.stdio, "stdio_client", lambda params: _Dies())
        await mgr._serve("alpha", object(), _entry(), ready, stop_event)

    before = mgr.revision
    asyncio.run(scenario())
    status = mgr.get_status()["alpha"]
    assert status["status"] == "error"
    assert "pipe closed" in status["error"]
    assert "mcp__alpha__ping" not in mgr._tools
    assert mgr.revision > before
