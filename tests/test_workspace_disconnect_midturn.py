"""Disconnect workspace is instant and never stops the session.

The report: clicking Disconnect while a session was working "does nothing and
freezes". The endpoint itself was cheap, but what came after it was not: the
running turn's next write paused it on a folder picker whose Browse button
blocked the event loop for as long as the dialog stayed open, a command still
running wrote the old workspace's cwd back into the next one, and a card
raised before the disconnect acted on whatever was connected when it was
approved. These pin the contract:

- the endpoint answers at once while a turn is busy and stops nothing;
- the running turn's next workspace tool call is refused with the reason (no
  pause, no folder picker) and the tool catalog does not change;
- processes the session started keep running;
- nothing from the disconnected workspace leaks into the next one;
- a second click and a quick reconnect both work;
- a failure is reported, never swallowed.
"""

import asyncio
import json
import os
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

import server.cwd_tracker as cwd_tracker
import server.executors.code  # noqa: F401 - registers run_python
import server.executors.terminal_run  # noqa: F401 - registers terminal_run
import server.git.executor  # noqa: F401 - registers the git tool executors
import server.search  # noqa: F401 - registers ws_grep / ws_glob
import server.tool_executor as te
import server.workspace.executors as ws_executors
import server.workspace.state as state
from server.cwd_tracker import get_cwd, update_cwd
from server.workspace.state import connect_workspace, latch_workspace

SESSION = "sess-disconnect"


@pytest.fixture(autouse=True)
def _clean_registries():
    cwd_tracker._session_cwd.clear()
    ws_executors._WORKTREES.clear()
    yield
    cwd_tracker._session_cwd.clear()
    ws_executors._WORKTREES.clear()


def _make_ws(root, name: str):
    ws = root / name
    (ws / "sub").mkdir(parents=True)
    (ws / "build").mkdir()
    (ws / "a.txt").write_text(f"{name} original\n")
    (ws / "build" / "out.txt").write_text(f"{name} build\n")
    return ws


@pytest.fixture
def ws_a(tmp_path):
    return _make_ws(tmp_path, "ws_a")


@pytest.fixture
def ws_b(tmp_path):
    return _make_ws(tmp_path, "ws_b")


def _app() -> FastAPI:
    from server.approval.bootstrap import register_defaults
    from server.approval.router import router as approval_router
    from server.workspace import router as ws_router

    register_defaults()
    app = FastAPI()
    app.include_router(ws_router)
    app.include_router(approval_router)
    return app


def _client(app: FastAPI | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or _app()), base_url="http://t")


class _LoopLag:
    """Largest event-loop stall seen while running (a 5 ms ticker's overshoot)."""

    def __init__(self) -> None:
        self.worst = 0.0
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        while not self._stop:
            t0 = time.perf_counter()
            await asyncio.sleep(0.005)
            self.worst = max(self.worst, time.perf_counter() - t0 - 0.005)

    def start(self) -> None:
        self._task = asyncio.create_task(self._tick())

    async def stop(self) -> float:
        self._stop = True
        await self._task
        return self.worst


# ── The endpoint: instant, and it stops nothing ──────────────────────────


def test_disconnect_answers_at_once_while_a_turn_is_busy(ws_a, ws_b):
    """A turn holds its stream slot and runs a command on the real tool pool.
    Disconnect answers in milliseconds, the loop never stalls, the command
    finishes untouched, a second click is harmless and a reconnect works."""
    from server.chat import executor as tool_pool
    from server.chat import stream_slot
    from server.workspace.executors import run_workspace_command

    real_a = connect_workspace(str(ws_a))

    async def scenario():
        token = time.monotonic()
        stream_slot.claim(SESSION, token, fresh=True)
        try:
            loop = asyncio.get_running_loop()
            command = loop.run_in_executor(
                tool_pool,
                lambda: run_workspace_command("cd sub && sleep 1", real_a, session_id=SESSION),
            )
            await asyncio.sleep(0.3)  # the command is running now
            lag = _LoopLag()
            lag.start()
            async with _client() as client:
                t0 = time.perf_counter()
                first = await client.post("/api/workspace/disconnect")
                elapsed = time.perf_counter() - t0
                status = (await client.get("/api/workspace/status")).json()
                second = await client.post("/api/workspace/disconnect")
                worst_lag = await lag.stop()
                slot_live = stream_slot.is_live(SESSION)

                run = await command

                reconnect = await client.post("/api/workspace/connect", json={"path": str(ws_b)})
            return first, elapsed, status, second, worst_lag, slot_live, run, reconnect
        finally:
            stream_slot.release(SESSION, token, stopped=False)

    first, elapsed, status, second, worst_lag, slot_live, run, reconnect = asyncio.run(scenario())

    assert first.status_code == 200, first.text
    assert first.json()["disconnected"] is True
    assert first.json()["warnings"] == []
    assert elapsed < 0.25, f"disconnect took {elapsed * 1000:.0f} ms while a turn was busy"
    assert worst_lag < 0.1, f"the event loop stalled {worst_lag * 1000:.0f} ms"
    assert status == {"connected": False}
    assert second.status_code == 200, "a second click must be harmless"
    assert slot_live, "disconnect must not end the running turn"
    assert run.returncode == 0 and not run.stopped, "disconnect stopped the running command"

    assert reconnect.status_code == 200, reconnect.text
    real_b = reconnect.json()["path"]
    assert state.get_workspace_path() == real_b
    # The command finished AFTER the disconnect; its `cd sub` in A must not
    # have become the session's cwd in B.
    assert get_cwd(SESSION, real_b) == real_b
    from server.git.watcher import git_watcher

    assert git_watcher._workspace == real_b


def test_processes_the_session_started_keep_running(ws_a, tmp_path, monkeypatch):
    from server.tasks import registry, shell

    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    connect_workspace(str(ws_a))
    task = shell.start_shell_task("sleep 30", cwd=str(ws_a), session_id=SESSION)
    try:

        async def disconnect():
            async with _client() as client:
                return await client.post("/api/workspace/disconnect")

        assert asyncio.run(disconnect()).status_code == 200
        time.sleep(0.2)
        assert registry.get_task(task["task_id"])["status"] == "running", (
            "disconnect killed a background task the session started"
        )
    finally:
        shell.stop_task(task["task_id"])


def test_a_failed_config_write_is_reported_and_leaves_the_workspace_connected(ws_a, monkeypatch):
    real_a = connect_workspace(str(ws_a))

    class _FullDisk:
        """state's json, except that writing runs out of space midway."""

        load = staticmethod(json.load)
        dumps = staticmethod(json.dumps)

        @staticmethod
        def dump(obj, f, **kwargs):
            f.write('{"pa')
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(state, "json", _FullDisk)

    async def go():
        async with _client() as client:
            return (
                await client.post("/api/workspace/disconnect"),
                (await client.get("/api/workspace/status")).json(),
            )

    resp, status = asyncio.run(go())
    assert resp.status_code == 500
    assert "No space left" in resp.json()["error"]
    assert "still connected" in resp.json()["error"]
    # The write is atomic, so the old config is whole and still authoritative,
    # and the half-written temp file is gone.
    assert status == {"connected": True, "path": real_a}
    config_dir = os.path.dirname(state.WORKSPACE_CONFIG_PATH)
    assert [n for n in os.listdir(config_dir) if n.endswith(".tmp")] == []


def test_a_failed_teardown_step_is_returned_as_a_warning(ws_a, monkeypatch):
    from server.git.watcher import git_watcher

    connect_workspace(str(ws_a))

    def boom(path):
        raise RuntimeError("watcher exploded")

    monkeypatch.setattr(git_watcher, "set_workspace", boom)

    async def go():
        async with _client() as client:
            return (
                await client.post("/api/workspace/disconnect"),
                (await client.get("/api/workspace/status")).json(),
            )

    resp, status = asyncio.run(go())
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert len(warnings) == 1 and "git watcher" in warnings[0] and "exploded" in warnings[0]
    assert status == {"connected": False}


def test_the_config_file_is_never_seen_half_written(ws_a):
    """get_workspace_path reads the file on every tool call; a truncate-then-
    write let a reader in a running turn see no workspace mid-write."""
    real_a = connect_workspace(str(ws_a))
    stop = threading.Event()
    seen = {"reads": 0, "none": 0}

    def reader():
        while not stop.is_set():
            seen["reads"] += 1
            if state.get_workspace_path() is None:
                seen["none"] += 1

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    try:
        for _ in range(1500):
            state.save_workspace_config({"path": real_a})
    finally:
        stop.set()
        for t in readers:
            t.join()
    assert seen["reads"] > 0
    assert seen["none"] == 0, f"{seen['none']} of {seen['reads']} reads saw no workspace"


def test_git_event_subscribers_hear_about_a_disconnect_at_once():
    from server.git.watcher import GitFileWatcher

    watcher = GitFileWatcher()
    heard = []
    watcher.subscribe(lambda: heard.append(watcher._workspace))
    watcher.set_workspace(None)
    assert heard == [None]


# ── The running turn: refused with the reason, never paused ──────────────


async def _run_batch(tool_uses: list[dict], latch):
    from concurrent.futures import ThreadPoolExecutor

    from server.approval.bootstrap import register_defaults

    register_defaults()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        states = await te.execute_tool_batch(
            tool_uses,
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=pool,
            transcript="",
            attachments=None,
            session_id=SESSION,
            session_denials={},
            model_id="",
            plan_mode=False,
            workspace_latch=latch,
        )
        results, sse, pending, question = await te.process_tool_results(
            states,
            budget_fn=lambda _name, out: out,
            session_approvals={},
            config={},
            model_id="",
            mode="default",
            workspace_latch=latch,
        )
        return states, results, sse, pending
    finally:
        pool.shutdown(wait=False)


_WORKSPACE_CALLS = [
    ("ws_read_file", {"path": "a.txt"}),
    ("ws_list_directory", {"path": "."}),
    ("ws_grep", {"pattern": "original"}),
    ("git_status", {}),
    ("ws_write_file", {"path": "a.txt", "content": "clobbered\n"}),
    ("ws_edit_file", {"path": "a.txt", "old_string": "original", "new_string": "edited"}),
    ("ws_delete_file", {"path": "a.txt"}),
    ("ws_create_file", {"path": "made.txt", "content": "new\n"}),
    ("ws_run_command", {"command": "ls"}),
    ("terminal_run", {"command": "ls"}),
    ("run_python", {"code": "print(1)"}),
]


def _uses(calls):
    return [{"id": f"t{i}", "name": n, "input": dict(inp)} for i, (n, inp) in enumerate(calls)]


def test_the_running_turn_is_refused_with_the_reason_after_disconnect(ws_a):
    connect_workspace(str(ws_a))
    latch = latch_workspace()  # the turn starts in A
    state.disconnect_workspace()  # the user clicks Disconnect mid-turn

    states, results, sse, pending = asyncio.run(_run_batch(_uses(_WORKSPACE_CALLS), latch))

    for st in states:
        assert st.output.startswith("No workspace connected: the user disconnected"), (
            st.tool_name,
            st.output[:200],
        )
        assert str(ws_a.name) in st.output
    assert pending is False, "the turn was paused instead of refused"
    assert not any("ws_workspace_prompt" in e or "approval_request" in e for e in sse)
    assert (ws_a / "a.txt").read_text() == "ws_a original\n"
    assert not (ws_a / "made.txt").exists()


def test_a_turn_that_never_had_a_workspace_still_gets_the_folder_picker():
    latch = latch_workspace()
    assert latch.path is None
    states, _results, sse, pending = asyncio.run(
        _run_batch(_uses([("ws_write_file", {"path": "a.txt", "content": "x"})]), latch)
    )
    assert states[0].output.startswith("[WS_WORKSPACE_PROMPT]")
    assert pending is True and any("ws_workspace_prompt" in e for e in sse)


def test_reconnecting_the_same_folder_gives_the_turn_its_tools_back(ws_a):
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    state.disconnect_workspace()
    connect_workspace(str(ws_a), by_user=True)

    states, *_ = asyncio.run(_run_batch(_uses([("ws_read_file", {"path": "a.txt"})]), latch))
    assert "ws_a original" in states[0].output


def test_switching_workspace_in_the_ui_releases_the_running_turn(ws_a, ws_b):
    """A card or a write meant for A must not land in B: the turn's paths are
    relative to the workspace it was assembled against."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    real_b = connect_workspace(str(ws_b), by_user=True)

    states, _results, _sse, pending = asyncio.run(
        _run_batch(
            _uses(
                [
                    ("ws_read_file", {"path": "a.txt"}),
                    ("ws_write_file", {"path": "a.txt", "content": "clobbered\n"}),
                ]
            ),
            latch,
        )
    )
    for st in states:
        assert st.output.startswith("No workspace connected"), st.output[:200]
        assert f"connected {real_b} instead" in st.output
    assert pending is False
    assert (ws_b / "a.txt").read_text() == "ws_b original\n"


def test_a_tool_that_opens_a_folder_does_not_release_its_own_turn(ws_a, ws_b):
    """git_clone and ws_open_folder connect a folder mid-turn on purpose; the
    same turn goes on working there."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    connect_workspace(str(ws_b))  # not by_user: a tool did it

    states, *_ = asyncio.run(_run_batch(_uses([("ws_read_file", {"path": "a.txt"})]), latch))
    assert "ws_b original" in states[0].output


def test_a_pinned_run_keeps_its_own_root_after_disconnect(ws_a, tmp_path):
    """Workflow runs and worktree-isolated agents are frozen to their root at
    launch and belong to the session, like its background shells."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    state.disconnect_workspace()

    latch_token = state.set_turn_latch(latch)
    pin_token = state.set_workspace_override(str(pinned))
    try:
        assert state.get_workspace_path() == str(pinned)
        assert state.workspace_lost_message() is None
    finally:
        state.reset_workspace_override(pin_token)
    try:
        # The same turn without the pin is refused.
        assert state.get_workspace_path() is None
        assert state.workspace_lost_message().startswith("No workspace connected")
    finally:
        state.reset_turn_latch(latch_token)


def test_the_tool_catalog_is_identical_across_connect_and_disconnect(ws_a):
    from server.chat.tool_pool import assemble_full_catalog

    connect_workspace(str(ws_a))
    connected = json.dumps(assemble_full_catalog(ws_connected=True), sort_keys=True)
    state.disconnect_workspace()
    disconnected = json.dumps(assemble_full_catalog(ws_connected=False), sort_keys=True)
    assert connected == disconnected


# ── Nothing from the disconnected workspace leaks into the next one ───────


def test_a_command_finishing_after_the_disconnect_cannot_write_its_cwd_back(ws_a, ws_b):
    real_a = connect_workspace(str(ws_a))
    started_at = cwd_tracker.generation()  # captured before the command starts
    state.disconnect_workspace()
    update_cwd(SESSION, os.path.join(real_a, "sub"), generation=started_at)
    real_b = connect_workspace(str(ws_b))
    assert get_cwd(SESSION, real_b) == real_b


def test_a_command_started_after_the_disconnect_still_tracks_its_cwd(ws_b):
    real_b = connect_workspace(str(ws_b))
    started_at = cwd_tracker.generation()
    update_cwd(SESSION, os.path.join(real_b, "sub"), generation=started_at)
    assert get_cwd(SESSION, real_b) == os.path.join(real_b, "sub")


def _raise_delete_card(ws_root) -> dict:
    """Raise a real ws_delete_file card through the gate; return its frame."""
    connect_workspace(str(ws_root))
    _states, _results, sse, pending = asyncio.run(
        _run_batch(_uses([("ws_delete_file", {"path": "build/out.txt"})]), latch_workspace())
    )
    assert pending is True
    cards = [json.loads(e)["approval_request"] for e in sse if "approval_request" in e]
    assert len(cards) == 1
    return cards[0]


async def _approve(card: dict) -> dict:
    async with _client() as client:
        resp = await client.post(
            "/api/approval/execute", json={"action": card["action"], "payload": card["payload"]}
        )
        return resp.json()


def test_a_card_raised_before_a_switch_cannot_act_on_the_new_workspace(ws_a, ws_b):
    card = _raise_delete_card(ws_a)
    assert card["payload"]["workspace_root"] == os.path.realpath(ws_a)

    state.disconnect_workspace()
    connect_workspace(str(ws_b), by_user=True)
    outcome = asyncio.run(_approve(card))

    assert outcome["ok"] is False
    assert "out of date" in outcome["error"] and os.path.realpath(ws_a) in outcome["error"]
    assert (ws_b / "build" / "out.txt").exists(), "the card deleted B's file"
    assert (ws_a / "build" / "out.txt").exists()


def test_a_card_approved_after_disconnect_changes_nothing(ws_a):
    card = _raise_delete_card(ws_a)
    state.disconnect_workspace()
    outcome = asyncio.run(_approve(card))
    assert outcome["ok"] is False
    assert "no longer connected" in outcome["error"]
    assert (ws_a / "build" / "out.txt").exists()


def test_a_card_approved_in_its_own_workspace_still_runs(ws_a):
    card = _raise_delete_card(ws_a)
    state.disconnect_workspace()
    connect_workspace(str(ws_a), by_user=True)  # the user came back to A
    outcome = asyncio.run(_approve(card))
    assert outcome["ok"] is True, outcome
    assert not (ws_a / "build" / "out.txt").exists()


# ── Nothing blocks the loop, so nothing can make the button look dead ─────


def test_the_folder_picker_does_not_block_other_requests(monkeypatch):
    """After a mid-turn disconnect the UI offered a folder picker whose Browse
    button ran osascript synchronously: the whole app froze while the native
    dialog was open. osascript is never run here."""

    class _Dialog:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(0.6)
            return b"/Users/someone/picked/\n", b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        assert args[0] == "osascript"
        return _Dialog()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    async def go():
        async with _client() as client:
            picker = asyncio.create_task(client.get("/api/workspace/pick-folder"))
            await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            status = await client.get("/api/workspace/status")
            waited = time.perf_counter() - t0
            still_open = not picker.done()
            return (await picker).json(), status.status_code, waited, still_open

    picked, status_code, waited, still_open = asyncio.run(go())
    assert status_code == 200
    assert still_open, "the dialog closed before the concurrent request was answered"
    assert waited < 0.2, f"/status waited {waited * 1000:.0f} ms behind the open dialog"
    assert picked == {"path": "/Users/someone/picked"}


def test_a_slow_file_search_does_not_block_other_requests(ws_a, monkeypatch):
    """The @-mention autocomplete fires a whole-workspace file search per
    keystroke, often mid-turn. The walk runs on a worker thread, so a
    Disconnect clicked meanwhile is answered at once."""
    import server.workspace.routes.browse as browse

    connect_workspace(str(ws_a))
    walking = threading.Event()

    def slow_search(root, query, max_results=100):
        walking.set()
        time.sleep(0.6)
        return [{"path": "a.txt", "binary": False}]

    monkeypatch.setattr(browse, "_ws_search_files", slow_search)

    async def go():
        async with _client() as client:
            search = asyncio.create_task(client.get("/api/workspace/search-files?q=a"))
            while not walking.is_set():
                await asyncio.sleep(0.01)
            t0 = time.perf_counter()
            disconnect = await client.post("/api/workspace/disconnect")
            waited = time.perf_counter() - t0
            still_walking = not search.done()
            return disconnect.status_code, waited, still_walking, (await search).json()

    status_code, waited, still_walking, found = asyncio.run(go())
    assert status_code == 200
    assert still_walking, "the search finished before the disconnect was answered"
    assert waited < 0.2, f"disconnect waited {waited * 1000:.0f} ms behind a file search"
    assert found == {"results": [{"path": "a.txt", "binary": False}]}


def test_no_async_handler_runs_a_blocking_subprocess_on_the_loop():
    """The class of bug behind the freeze: an `async def` that calls
    subprocess.run (or time.sleep, os.system, a directory walk) directly holds
    the one event loop, so every stream and every click waits. Hand it to a
    thread (asyncio.to_thread) or use asyncio's own subprocess API."""
    import ast
    import pathlib

    blocking = {
        ("subprocess", "run"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("time", "sleep"),
        ("os", "system"),
        ("os", "walk"),
    }
    # The workspace's own tree walkers: the file search walks every folder,
    # and the file-tree endpoints list a whole directory per call.
    blocking_helpers = {"_ws_search_files", "_ws_list_dir"}
    root = pathlib.Path(__file__).resolve().parent.parent / "server"
    offenders = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, rel):
            self.rel = rel
            self.stack: list[bool] = []

        def visit_AsyncFunctionDef(self, node):
            self.stack.append(True)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.stack.append(False)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Lambda(self, node):
            self.stack.append(False)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            func = node.func
            if self.stack and self.stack[-1]:
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and (func.value.id, func.attr) in blocking
                ):
                    offenders.append(f"{self.rel}:{node.lineno} {func.value.id}.{func.attr}")
                elif isinstance(func, ast.Name) and func.id in blocking_helpers:
                    offenders.append(f"{self.rel}:{node.lineno} {func.id}")
            self.generic_visit(node)

    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        _Visitor(path.relative_to(root.parent)).visit(ast.parse(path.read_text()))
    assert not offenders, "blocking calls inside async handlers:\n" + "\n".join(offenders)
