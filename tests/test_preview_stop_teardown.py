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
