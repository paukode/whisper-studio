"""Stop stops what the stopped turn started, and nothing from earlier turns.

The kill switch (POST /api/workspace/shell/tasks/stop) used to kill every
running shell task of the session, including a dev server a previous turn had
started on purpose, with a blocking kill on the event loop. It also could not
see a long foreground command during its first 30 s (no registry row yet), so
such a command outlived the Stop and then turned into a background task of the
stopped session.
"""

import asyncio
import contextlib
import os
import signal
import threading
import time

import pytest

from server.tasks import handoff, registry, shell


@pytest.fixture(autouse=True)
def isolated_output(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))


def _wait_status(task_id: str, status: str, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get_task(task_id)
        if task and task["status"] == status:
            return True
        time.sleep(0.05)
    return False


def _in_thread(fn):
    box: dict = {}

    def run():
        box["result"] = fn()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_stop_spares_tasks_from_earlier_turns(tmp_path):
    earlier = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s-scope")
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    this_turn = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s-scope")
    try:
        result = handoff.stop_turn_work("s-scope", since)
        assert this_turn["task_id"] in result["stopped"]
        assert earlier["task_id"] not in result["stopped"]
        assert _wait_status(this_turn["task_id"], "stopped")
        assert registry.get_task(earlier["task_id"])["status"] == "running", (
            "Stop killed a task an earlier turn started"
        )
    finally:
        shell.stop_task(earlier["task_id"])
        _wait_status(earlier["task_id"], "stopped")


def test_stop_never_touches_another_sessions_tasks(tmp_path):
    since = time.time()
    other = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s-other")
    try:
        assert handoff.stop_turn_work("s-mine", since)["stopped"] == []
        assert registry.get_task(other["task_id"])["status"] == "running"
    finally:
        shell.stop_task(other["task_id"])
        _wait_status(other["task_id"], "stopped")


def test_stop_kills_a_foreground_command_before_its_handoff(tmp_path):
    """Inside its 30 s budget a command has no registry row; Stop must still
    reach it, and it must never reappear later as a background task."""
    since = time.time()
    t, box = _in_thread(
        lambda: handoff.run_with_handoff(
            "sleep 30", "sleep 30", cwd=str(tmp_path), session_id="s-fg", timeout=5
        )
    )
    time.sleep(0.3)
    started = time.monotonic()
    result = handoff.stop_turn_work("s-fg", since)
    t.join(timeout=5)
    assert not t.is_alive(), "the foreground command survived the Stop"
    assert time.monotonic() - started < 4
    assert result["foreground"] == 1
    out = box["result"]
    assert out.background is False
    assert "stopped" in out.output
    assert registry.list_tasks(session_id="s-fg") == []


def test_stop_landing_as_the_budget_expires_never_adopts_the_process(tmp_path, monkeypatch):
    """A kill still in progress when the foreground budget runs out: the
    handoff must not register the dying process as a new running task."""
    from server import process_utils

    def slow_kill(proc, timeout=None):
        time.sleep(0.6)  # the budget (0.3 s) expires while this kill runs
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    monkeypatch.setattr(process_utils, "kill_process_group", slow_kill)
    since = time.time()
    t, box = _in_thread(
        lambda: handoff.run_with_handoff(
            "sleep 30", "sleep 30", cwd=str(tmp_path), session_id="s-race", timeout=0.3
        )
    )
    time.sleep(0.1)
    handoff.stop_turn_work("s-race", since)
    t.join(timeout=5)
    assert not t.is_alive()
    assert box["result"].background is False
    assert registry.list_tasks(session_id="s-race") == [], "a stopped command was adopted"


def test_a_handed_off_task_of_this_turn_is_stopped(tmp_path):
    since = time.time()
    result = handoff.run_with_handoff(
        "sleep 30", "sleep 30", cwd=str(tmp_path), session_id="s-ho", timeout=0.2
    )
    assert result.background
    assert result.task_id in handoff.stop_turn_work("s-ho", since)["stopped"]
    assert _wait_status(result.task_id, "stopped")


class _JsonRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


def test_stop_endpoint_requires_the_turn_start():
    from server.workspace.routes.shell import ws_shell_tasks_stop

    resp = asyncio.run(ws_shell_tasks_stop(_JsonRequest({"session_id": "s"})))
    assert resp.status_code == 400
    resp = asyncio.run(ws_shell_tasks_stop(_JsonRequest({"session_id": "s", "since": True})))
    assert resp.status_code == 400


def test_stop_endpoint_kills_off_the_event_loop(tmp_path, monkeypatch):
    """A process that ignores SIGTERM makes the kill wait out its grace
    period; other sessions' coroutines must keep running meanwhile."""
    from server.workspace.routes.shell import ws_shell_tasks_stop

    def slow_stop(session_id, since):
        time.sleep(0.6)
        return {"stopped": [], "foreground": 0}

    monkeypatch.setattr(handoff, "stop_turn_work", slow_stop)

    async def scenario() -> int:
        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.05)
                ticks += 1

        tt = asyncio.create_task(ticker())
        await ws_shell_tasks_stop(_JsonRequest({"session_id": "s", "since": time.time()}))
        done.set()
        await tt
        return ticks

    assert asyncio.run(scenario()) >= 5


# ── Another run in the same session keeps its own work ───────────────────────


def _owned_in_thread(owner_id: str, fn):
    """Run ``fn`` in a worker thread the way a run's tool call does (the tool
    router's executor hop carries the run's context), under ``owner_id``."""
    from server.tasks.owner import run_owned

    return _in_thread(lambda: asyncio.run(run_owned(owner_id, asyncio.to_thread(fn))))


def test_chat_stop_spares_a_command_another_run_started_in_the_window(tmp_path):
    """A detached agent, a workflow, a voice-delegated run or a cron job runs
    its tools under the chat session id. The chat's Stop must not kill what
    that run started, even inside the stopped turn's window; the chat turn's
    own command in the same window still dies."""
    sid = "s-owner"
    since = time.time()
    time.sleep(0.05)
    agent_t, agent_box = _owned_in_thread(
        "agent:a1",
        lambda: handoff.run_with_handoff(
            "sleep 3", "sleep 3", cwd=str(tmp_path), session_id=sid, timeout=30
        ),
    )
    chat_t, chat_box = _in_thread(
        lambda: handoff.run_with_handoff(
            "sleep 30", "sleep 30", cwd=str(tmp_path), session_id=sid, timeout=30
        )
    )
    time.sleep(0.3)
    result = handoff.stop_turn_work(sid, since)
    chat_t.join(10)
    agent_t.join(10)
    assert result["foreground"] == 1
    assert handoff._STOPPED_NOTE in chat_box["result"].output
    assert agent_box["result"].returncode == 0
    assert handoff._STOPPED_NOTE not in agent_box["result"].output


def test_chat_stop_spares_another_runs_background_task(tmp_path):
    sid = "s-owner-bg"
    since = time.time()
    time.sleep(0.05)
    t, box = _owned_in_thread(
        "workflow:w1",
        lambda: shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id=sid),
    )
    t.join(5)
    theirs = box["result"]["task_id"]
    mine = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id=sid)
    try:
        result = handoff.stop_turn_work(sid, since)
        assert mine["task_id"] in result["stopped"]
        assert theirs not in result["stopped"]
        assert registry.get_task(theirs)["status"] == "running"
    finally:
        shell.stop_task(theirs)
        shell.stop_task(mine["task_id"])


def test_a_detached_agent_runs_its_tools_as_its_own_work(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from server.tasks import agents as tagents
    from server.tasks import owner

    seen = {}

    async def _fake_run_agent(task, **kwargs):
        seen["owner"] = owner.current()
        return SimpleNamespace(output="done", status="completed")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    monkeypatch.setattr(tagents, "emit_agent_report", lambda *a, **k: None)
    monkeypatch.setattr(tagents, "emit_task_event", lambda *a, **k: None)

    async def _go():
        tid = tagents.start_detached_agent("look around", session_id="s-det")
        await tagents._running[tid]
        return tid

    tid = asyncio.run(_go())
    assert seen["owner"] == f"agent:{tid}"
    assert owner.current() == ""


def test_stopping_a_run_kills_only_the_commands_it_owns(tmp_path):
    sid = "s-owned-stop"
    theirs_t, theirs = _owned_in_thread(
        "subagent:t1",
        lambda: handoff.run_with_handoff(
            "sleep 30", "sleep 30", cwd=str(tmp_path), session_id=sid, timeout=30
        ),
    )
    other_t, other = _owned_in_thread(
        "subagent:t2",
        lambda: handoff.run_with_handoff(
            "sleep 1", "sleep 1", cwd=str(tmp_path), session_id=sid, timeout=30
        ),
    )
    chat_t, chat = _in_thread(
        lambda: handoff.run_with_handoff(
            "sleep 1", "sleep 1", cwd=str(tmp_path), session_id=sid, timeout=30
        )
    )
    time.sleep(0.3)
    result = handoff.stop_owned_work(sid, "subagent:t1")
    for t in (theirs_t, other_t, chat_t):
        t.join(10)
    assert result["foreground"] == 1
    assert handoff._STOPPED_NOTE in theirs["result"].output
    assert other["result"].returncode == 0 and chat["result"].returncode == 0


def test_aborting_a_subagent_run_kills_the_command_it_is_running(tmp_path, monkeypatch):
    """ESC or the card's Stop aborts the /subagent stream. Cancelling the
    agent cannot stop a command in a worker thread; the route kills the run's
    own commands, and nothing of the chat's."""
    from server.chat import routes
    from tests.local_chat_stack import FakeRequest

    sid = "s-subagent-abort"
    box: dict = {}
    started = threading.Event()

    async def _fake_run_agent(task, **kwargs):
        def run():
            started.set()
            box["result"] = handoff.run_with_handoff(
                "sleep 30", "sleep 30", cwd=str(tmp_path), session_id=sid, timeout=30
            )

        await asyncio.to_thread(run)

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    monkeypatch.setattr(routes, "subagent_refusal", lambda key: None)
    monkeypatch.setattr(routes, "_get_chat_models", lambda *a: {"m": "model-id"})

    async def go():
        resp = await routes.subagent_stream_endpoint(
            FakeRequest({"task": "start the dev server", "model": "m", "session_id": sid})
        )

        async def consume():
            async for _chunk in resp.body_iterator:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        consumer.cancel()  # the client aborted the stream
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        while "result" not in box and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    assert handoff._STOPPED_NOTE in box["result"].output
    assert elapsed < 8


def test_an_approved_command_that_stop_ended_says_so(tmp_path, monkeypatch):
    """The approval card's outcome row must say Stop ended the command, not
    that it failed with a signal exit code."""
    from server.approval.executors import _do_command

    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    sid = "s-approved-stop"
    since = time.time()

    async def go():
        run = asyncio.create_task(_do_command({"command": "sleep 30", "session_id": sid}))
        await asyncio.sleep(0.4)
        await asyncio.to_thread(handoff.stop_turn_work, sid, since)
        return await asyncio.wait_for(run, 10)

    outcome = asyncio.run(go())
    assert outcome.stopped is True and outcome.ok is False
    assert "exit code" not in (outcome.error or "")
