"""A preview_stop cancelled mid-teardown still stops the dev server.

stop_session pops the preview from the registry first, then tears it down.
It used to await the browser close and only then kill the dev server, inline
in the caller. An auto-approved preview_stop runs inside the turn, so a Stop
landing during that teardown cancelled it: the server was never signalled,
kept its port, and no longer appeared anywhere a tool or the UI could stop it.
"""

import asyncio

import pytest

from server.preview.manager import PreviewManager, PreviewSession


class _Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.done = False

    async def run(self):
        self.entered.set()
        await self.release.wait()
        self.done = True


class _FakeProcess:
    alive = True

    def __init__(self, gate: _Gate | None = None):
        self.gate = gate
        self.stop_called = False
        self.stopped = False

    async def stop(self):
        self.stop_called = True
        if self.gate:
            await self.gate.run()
        self.stopped = True


class _FakeBrowser:
    page = None

    def __init__(self, gate: _Gate | None = None):
        self.gate = gate
        self.closed = False

    async def close(self):
        if self.gate:
            await self.gate.run()
        self.closed = True


async def _cancel_mid_teardown(process: _FakeProcess, browser: _FakeBrowser, gate: _Gate):
    mgr = PreviewManager()
    mgr._sessions["dev"] = PreviewSession(id="dev", process=process, browser=browser, owner="a")
    caller = asyncio.create_task(mgr.stop_session("dev", caller="a"))
    await asyncio.wait_for(gate.entered.wait(), timeout=2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    gate.release.set()
    for _ in range(50):
        if process.stopped and browser.closed:
            break
        await asyncio.sleep(0.01)
    return mgr


def test_cancel_during_the_kill_still_finishes_the_teardown():
    async def scenario():
        gate = _Gate()
        process, browser = _FakeProcess(gate), _FakeBrowser()
        mgr = await _cancel_mid_teardown(process, browser, gate)
        assert process.stopped, "the dev server kill was abandoned by the cancel"
        assert browser.closed, "the preview browser was left running"
        assert mgr.get("dev") is None

    asyncio.run(scenario())


def test_cancel_during_the_browser_close_has_already_signalled_the_server():
    async def scenario():
        gate = _Gate()
        process, browser = _FakeProcess(), _FakeBrowser(gate)
        await _cancel_mid_teardown(process, browser, gate)
        assert process.stop_called and process.stopped, "the dev server was never stopped"
        assert browser.closed

    asyncio.run(scenario())


def _alive(pid: int) -> bool:
    import os
    import subprocess

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reparented zombie still answers kill(0); ps knows better.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_app_quit_stops_every_preview_and_the_children_it_started(tmp_path):
    """Quit runs preview_manager.stop_all (the lifespan in server/main.py).
    Each dev server leads its own process group, so the stop also reaches
    what the server started (a bundler, a watcher), not only its own pid."""
    from server.preview.process import DevServerProcess

    async def scenario():
        manager = PreviewManager()
        started = []
        for name in ("web", "docs"):
            pidfile = tmp_path / f"{name}.pid"
            proc = await asyncio.create_subprocess_exec(
                "sh",
                "-c",
                f'sleep 60 & echo $! > "{pidfile}"; wait',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            server = DevServerProcess(proc, ["sh"], str(tmp_path), None)
            manager._sessions[name] = PreviewSession(id=name, process=server)
            started.append((proc.pid, pidfile))
        for _pid, pidfile in started:
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                await asyncio.sleep(0.02)
        children = [int(pidfile.read_text()) for _pid, pidfile in started]
        await manager.stop_all()
        return [pid for pid, _f in started], children, manager.list_sessions()

    leaders, children, left = asyncio.run(scenario())
    assert left == []
    for pid in leaders + children:
        assert _wait_dead(pid), f"pid {pid} survived the quit"
