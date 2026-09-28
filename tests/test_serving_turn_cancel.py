"""A turn cancelled while its on-device model loads must not leak a busy mark.

``ensure_serving(mark_busy=True)`` registers the turn from the worker thread
when the load lands. Stop (or a closed tab) during a cold start cancelled the
awaiting task first, so the turn's ``end_turn`` finally was never entered and
the late ``begin_turn`` stayed counted forever: every later model switch or
context change then waited ten minutes for a turn that did not exist.
``serving.serve_turn`` owns that pairing for the chat route and the agent
runtime.
"""

import asyncio
import threading
import time

import pytest

from server.local import llama_server, mlx_server, runtime, serving


@pytest.fixture(autouse=True)
def _reset_busy():
    while serving.busy_turns() > 0:
        serving.end_turn()
    yield
    while serving.busy_turns() > 0:
        serving.end_turn()


def _slow_load(monkeypatch, seconds=0.4):
    """A cold start that takes ``seconds`` and then marks the turn, the way
    the real ensure_serving does under mark_busy."""
    done = threading.Event()
    seen: dict = {}

    def fake(key, n_ctx=None, *, should_abort=None, mark_busy=False):
        seen["should_abort"] = should_abort
        time.sleep(seconds)
        if mark_busy:
            serving.begin_turn()
        done.set()
        return "http://127.0.0.1:1"

    monkeypatch.setattr(serving, "ensure_serving", fake)
    return done, seen


async def _settle(done: threading.Event):
    # The release runs as a done-callback on this loop once the thread returns.
    for _ in range(100):
        if done.is_set():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


def test_cancel_during_cold_start_releases_the_late_mark(monkeypatch):
    done, _ = _slow_load(monkeypatch)
    before = serving.busy_turns()

    async def go():
        task = asyncio.create_task(serving.serve_turn("local_x"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle(done)

    asyncio.run(go())
    assert done.is_set()
    assert serving.busy_turns() == before


def test_completed_load_leaves_exactly_one_mark_for_the_caller(monkeypatch):
    _slow_load(monkeypatch, seconds=0.05)
    before = serving.busy_turns()

    async def go():
        return await serving.serve_turn("local_x")

    assert asyncio.run(go()) == "http://127.0.0.1:1"
    assert serving.busy_turns() == before + 1
    serving.end_turn()


def test_stop_during_the_route_cold_start_releases_the_mark(monkeypatch):
    """The chat route end to end: a local turn whose stream is cancelled while
    llama-server is still starting."""
    from server.local import route as local_route

    monkeypatch.setattr(runtime, "is_local_model", lambda k: True)
    monkeypatch.setattr(runtime, "supports_tools", lambda k: False)
    monkeypatch.setattr(runtime, "supports_thinking", lambda k: False)
    monkeypatch.setattr(runtime, "build_local_system_prompt", lambda *a, **k: "sys")
    done, _ = _slow_load(monkeypatch)
    before = serving.busy_turns()

    resp = local_route.local_chat_response(
        model_key="local_x",
        body={},
        messages=[{"role": "user", "content": "hey"}],
        session_id="cold-stop",
        approved_tool_result=None,
        whisper_md_context="",
        memory_context="",
        session_memory_context="",
        plan_mode=False,
        mode="default",
        ws_path=None,
        session_approvals={},
        session_denials={},
        session_config={},
    )

    async def go():
        async def consume():
            async for _ in resp.body_iterator:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle(done)

    asyncio.run(go())
    assert serving.busy_turns() == before


def _stub_engines(monkeypatch, resident, stops):
    monkeypatch.setattr(runtime, "LOCAL_MODELS", {"model_a": {}, "model_b": {}})
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda k: "/w")
    monkeypatch.setattr(llama_server, "resident_key", lambda: resident)
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: 4096)
    monkeypatch.setattr(llama_server, "base_url", lambda: "http://a")
    monkeypatch.setattr(llama_server, "is_running", lambda: resident is not None)
    monkeypatch.setattr(llama_server, "ensure_available", lambda: None)
    monkeypatch.setattr(llama_server, "stop", lambda: stops.append("llama"))
    monkeypatch.setattr(
        llama_server, "ensure_serving", lambda k, n=None: stops.append(f"serve:{k}") or "http://b"
    )
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "is_running", lambda: False)
    monkeypatch.setattr(mlx_server, "stop", lambda: stops.append("mlx"))


def test_cancel_while_waiting_for_another_turn_aborts_the_switch(monkeypatch):
    """A stopped turn that was waiting for another session's turn must not
    displace that model later: the cancel aborts the wait itself."""
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    serving.begin_turn()  # another session streams from model_a

    async def go():
        task = asyncio.create_task(serving.serve_turn("model_b"))
        await asyncio.sleep(0.3)  # waiting on model_a's live turn
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.6)  # a few poll ticks for the worker to see it

    asyncio.run(go())
    assert serving.busy_turns() == 1  # only model_a's own turn
    serving.end_turn()  # model_a's turn finishes
    time.sleep(0.4)
    assert stops == []  # nothing was stopped or started for the cancelled turn


def test_cancel_while_queued_behind_another_load_claims_nothing(monkeypatch):
    """A turn for model_a that arrives while model_b's load holds the
    transition lock queues behind that load. Stopped there, it had no wait
    loop to see the cancel, and it used to evict the freshly loaded model_b
    to cold-load model_a for a turn that no longer existed."""
    events: list[str] = []
    state = {"resident": "model_a"}
    warming = threading.Event()
    release = threading.Event()

    def _serve(k, n=None):
        if state["resident"] != k:
            events.append(f"start:{k}")
            if k == "model_b":
                warming.set()
                release.wait(5)
            state["resident"] = k
        return f"http://{k}"

    monkeypatch.setattr(runtime, "LOCAL_MODELS", {"model_a": {}, "model_b": {}})
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda k: "/w")
    monkeypatch.setattr(llama_server, "resident_key", lambda: state["resident"])
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: 4096)
    monkeypatch.setattr(llama_server, "base_url", lambda: f"http://{state['resident']}")
    monkeypatch.setattr(llama_server, "is_running", lambda: True)
    monkeypatch.setattr(llama_server, "ensure_available", lambda: None)
    monkeypatch.setattr(llama_server, "stop", lambda: events.append("stop"))
    monkeypatch.setattr(llama_server, "ensure_serving", _serve)
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "is_running", lambda: False)
    monkeypatch.setattr(mlx_server, "stop", lambda: events.append("mlx-stop"))

    load_b = threading.Thread(target=lambda: serving.ensure_serving("model_b"), daemon=True)
    load_b.start()
    assert warming.wait(5)  # model_b's load holds the lock, model_a still resident

    async def go():
        task = asyncio.create_task(serving.serve_turn("model_a"))
        await asyncio.sleep(0.3)  # refused by the fast path, queued on the lock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()  # model_b comes up and its load frees the lock
        await asyncio.sleep(0.3)

    asyncio.run(go())
    load_b.join(5)
    assert events == ["start:model_b"]  # nothing started for the stopped turn
    assert state["resident"] == "model_b" and serving.busy_turns() == 0
