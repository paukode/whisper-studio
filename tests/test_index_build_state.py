"""One record of "is this workspace building", whoever started the build.

/status used to read only the builds the Build button started, while /build
also refused on the scheduled refresh's lock: the panel said "not building"
and the button said "already building" at the same time.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from server.index import build_state, pipeline, routes, scheduler


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def gated_build(monkeypatch):
    """Replace the indexing work with one that reports progress, then waits
    for the test to let it finish (or fail)."""
    started = threading.Event()
    release = threading.Event()
    outcome: dict = {}

    def fake(ws_path, ws_root, progress):
        progress(1, 3, "a.py")
        started.set()
        assert release.wait(5)
        if outcome.get("error"):
            raise RuntimeError(outcome["error"])
        return {"files": 3}

    monkeypatch.setattr(pipeline, "_build_claimed", fake)
    monkeypatch.setattr(build_state, "_builds", {})
    return started, release, outcome


def _wait_idle(path: str) -> None:
    for _ in range(500):
        if not build_state.is_building(path):
            return
        threading.Event().wait(0.01)
    raise AssertionError("build did not finish")


def test_a_scheduled_build_is_building_for_status_and_build(tmp_path, gated_build):
    started, release, _ = gated_build
    ws = str(tmp_path)
    scheduler._run_one(ws)
    assert started.wait(5)

    status = asyncio.run(routes.index_status(ws))
    assert status["building"] is True
    assert status["progress"] == {"done": 1, "total": 3, "current": "a.py"}
    assert asyncio.run(routes.index_build(_Req({"path": ws}))) == {
        "started": False,
        "reason": "already building",
    }
    # A direct build of the same workspace does not run a second one either.
    assert pipeline.build(ws) == {"skipped": "already building"}

    release.set()
    _wait_idle(ws)
    status = asyncio.run(routes.index_status(ws))
    assert status["building"] is False and status["error"] is None
    assert asyncio.run(routes.index_build(_Req({"path": ws}))) == {"started": True}
    _wait_idle(ws)


def test_a_failed_build_reports_its_error_until_the_next_one(tmp_path, gated_build):
    started, release, outcome = gated_build
    ws = str(tmp_path)
    outcome["error"] = "embedder crashed"
    assert asyncio.run(routes.index_build(_Req({"path": ws}))) == {"started": True}
    assert started.wait(5)
    release.set()
    _wait_idle(ws)
    assert build_state.status(ws)["error"] == "embedder crashed"

    outcome.clear()
    assert pipeline.build(ws) == {"files": 3}
    assert build_state.status(ws)["error"] is None


def test_forget_never_hides_a_running_build(tmp_path, gated_build):
    started, release, _ = gated_build
    ws = str(tmp_path)
    assert pipeline.start_build(ws) is True
    assert started.wait(5)
    build_state.forget(ws)
    assert build_state.is_building(ws) is True
    release.set()
    _wait_idle(ws)
    build_state.forget(ws)
    assert build_state.status(ws) == build_state.IDLE


def test_a_cancel_before_the_first_file_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(build_state, "_builds", {})
    root = build_state.ws_root(str(tmp_path))
    assert build_state.request_cancel(str(tmp_path)) is False  # nothing to cancel
    assert build_state.claim(root) is True
    assert build_state.request_cancel(str(tmp_path)) is True
    # The build loop reads this event; the claim cleared it, nothing after.
    assert build_state.cancel_event(root).is_set()
    build_state.note(root, building=False, error=None)
    assert build_state.claim(root) is True
    assert not build_state.cancel_event(root).is_set()
