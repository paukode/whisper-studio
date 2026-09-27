"""A cancelled /api/local-model/load stops what IT started, and nothing else.

Two field races on the loading banner's Cancel:

- Moving the CTX slider reloads the SAME model at a new size, so the load
  waits for another session's live turn. Cancelling it used to stop the old
  server that turn was streaming from, because the stop only compared the
  model key.
- The banner posts the cancel and then aborts the stream. When the POST landed
  first, the stream's cleanup cleared the flag before the waiting worker saw
  it, and the cancelled switch happened anyway once the live turn ended.

These drive the real ``serving.ensure_serving`` over stubbed engines.
"""

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.chat.routes as routes_mod
from server.local import llama_server, mlx_server, runtime, serving
from server.models_manager import catalog


@pytest.fixture(autouse=True)
def _clean_state():
    while serving.busy_turns() > 0:
        serving.end_turn()
    yield
    while serving.busy_turns() > 0:
        serving.end_turn()
    with routes_mod._load_cancels_lock:
        routes_mod._load_cancels.clear()


def _stub(monkeypatch, *, resident="model_a", n_ctx=4096, serve_s=0.0):
    """One llama engine serving ``resident`` at ``n_ctx``; records every stop
    and start with the live-turn count at that moment."""
    events: list[tuple[str, int]] = []
    state = {"resident": resident, "n_ctx": n_ctx}
    models = {"model_a": {}, "model_b": {}}
    monkeypatch.setattr(runtime, "LOCAL_MODELS", models)
    monkeypatch.setattr(runtime, "is_local_model", lambda k: k in models)
    monkeypatch.setattr(runtime, "is_downloaded", lambda k: True)
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda k: "/w")
    monkeypatch.setattr(runtime, "local_model_meta", lambda k: {"label": k})
    monkeypatch.setattr(runtime, "set_requested_n_ctx", lambda n: None)
    monkeypatch.setattr(catalog, "get_entry", lambda k: None)

    def _serve(k, n=None):
        events.append((f"start:{k}", serving.busy_turns()))
        time.sleep(serve_s)
        state.update(resident=k, n_ctx=n or state["n_ctx"])
        return f"http://{k}"

    def _stop():
        events.append(("stop", serving.busy_turns()))
        state["resident"] = None

    monkeypatch.setattr(llama_server, "resident_key", lambda: state["resident"])
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: state["n_ctx"])
    monkeypatch.setattr(
        llama_server, "base_url", lambda: state["resident"] and f"http://{state['resident']}"
    )
    monkeypatch.setattr(llama_server, "is_running", lambda: state["resident"] is not None)
    monkeypatch.setattr(llama_server, "ensure_available", lambda: None)
    monkeypatch.setattr(llama_server, "stop", _stop)
    monkeypatch.setattr(llama_server, "ensure_serving", _serve)
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "is_running", lambda: False)
    monkeypatch.setattr(mlx_server, "stop", lambda: None)
    return events, state


def _client():
    app = FastAPI()
    app.include_router(routes_mod.router)
    return TestClient(app)


def _last_stage(text: str) -> str:
    import json

    frames = [
        json.loads(ln[6:])
        for ln in text.splitlines()
        if ln.startswith("data: ") and ln != "data: [DONE]"
    ]
    return frames[-1]["stage"]


def test_cancelled_ctx_reload_leaves_the_live_turns_server_alone(monkeypatch):
    events, state = _stub(monkeypatch, resident="model_a", n_ctx=4096)
    serving.begin_turn()  # another session streams from model_a at 4096
    t0 = time.monotonic()
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: time.monotonic() - t0 > 0.3)

    resp = _client().get("/api/local-model/load?model=model_a&n_ctx=8192")

    assert _last_stage(resp.text) == "cancelled"
    assert events == []  # the old-size server was never stopped or restarted
    assert state["resident"] == "model_a" and serving.busy_turns() == 1


def test_cancelled_load_of_the_resident_model_stops_nothing(monkeypatch):
    """Nothing to undo: the model was already resident at that size."""
    events, state = _stub(monkeypatch, resident="model_a", n_ctx=4096)
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: True)

    resp = _client().get("/api/local-model/load?model=model_a&n_ctx=4096")

    assert _last_stage(resp.text) == "cancelled"
    assert events == [] and state["resident"] == "model_a"


def test_cancelled_switch_that_did_start_is_unloaded(monkeypatch):
    """The contract the scoping keeps: a cancel that lands once the load is
    spawning its server still undoes it, so the cancelled model is not left
    resident."""
    events, state = _stub(monkeypatch, resident="model_a", serve_s=0.3)
    serve = llama_server.ensure_serving
    spawning = threading.Event()

    def _serve(k, n=None):
        spawning.set()
        return serve(k, n)

    monkeypatch.setattr(llama_server, "ensure_serving", _serve)
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: spawning.is_set())

    resp = _client().get("/api/local-model/load?model=model_b")

    assert _last_stage(resp.text) == "cancelled"
    assert ("start:model_b", 0) in events
    assert events[-1][0] == "stop" and state["resident"] is None


def test_switch_cancelled_before_it_claims_starts_nothing(monkeypatch):
    """A cancel that is already standing when the load reaches the transition
    lock (it was queued behind another load) leaves the resident model be."""
    events, state = _stub(monkeypatch, resident="model_a")
    monkeypatch.setattr(routes_mod, "_load_cancel_requested", lambda m: True)

    resp = _client().get("/api/local-model/load?model=model_b")

    assert _last_stage(resp.text) == "cancelled"
    assert events == [] and state["resident"] == "model_a"


def _open_load(model):
    """Open the load stream and step into its wait loop (two frames: the
    stage frame, then the first poll tick with the worker running)."""

    async def opened():
        resp = await routes_mod.local_model_load(model=model)
        it = resp.body_iterator
        await it.__anext__()
        await it.__anext__()
        return it

    return opened()


def test_cancel_posted_before_the_disconnect_still_stops_the_waiting_load(monkeypatch):
    events, state = _stub(monkeypatch, resident="model_a")
    serving.begin_turn()  # another session streams from model_a

    async def run():
        it = await _open_load("model_b")  # waits on model_a's live turn
        with routes_mod._load_cancels_lock:  # the banner's cancel POST lands ...
            routes_mod._load_cancels.add("model_b")
        await it.aclose()  # ... then its aborted fetch closes the stream
        await asyncio.sleep(0.7)  # the worker polls should_abort every 0.25 s
        serving.end_turn()  # model_a's turn finishes
        await asyncio.sleep(0.6)

    asyncio.run(run())
    assert events == []  # model_b was never started, model_a never stopped
    assert state["resident"] == "model_a"
    with routes_mod._load_cancels_lock:
        assert "model_b" not in routes_mod._load_cancels  # settled, not stranded


def test_cancel_posted_before_the_disconnect_undoes_a_warming_load(monkeypatch):
    """Past the busy wait nothing can interrupt the worker; once its server is
    up, the standing cancel stops it."""
    events, state = _stub(monkeypatch, resident=None, serve_s=0.8)

    async def run():
        it = await _open_load("model_b")  # warming model_b
        with routes_mod._load_cancels_lock:
            routes_mod._load_cancels.add("model_b")
        await it.aclose()
        for _ in range(60):
            if events and events[-1][0] == "stop":
                break
            await asyncio.sleep(0.05)

    asyncio.run(run())
    assert [name for name, _ in events] == ["start:model_b", "stop"]
    assert state["resident"] is None


def test_disconnect_while_awaiting_a_cancelled_load_keeps_the_cancel(monkeypatch):
    """The stream's poll tick can see the cancel first, then wait for the
    worker, which looks at should_abort only every 0.25 s. The aborted fetch
    landing during that wait used to cancel the load future, so the cleanup
    took the worker for finished and dropped the flag it had yet to see."""
    events, state = _stub(monkeypatch, resident="model_a")
    serving.begin_turn()  # another session streams from model_a
    worker_may_look = threading.Event()
    real = serving.ensure_serving

    def held_back(key, n_ctx=None, *, should_abort=None, **kw):
        return real(
            key, n_ctx, should_abort=lambda: worker_may_look.is_set() and should_abort(), **kw
        )

    monkeypatch.setattr(serving, "ensure_serving", held_back)

    async def run():
        it = await _open_load("model_b")  # waits on model_a's live turn
        with routes_mod._load_cancels_lock:  # the banner's cancel POST lands
            routes_mod._load_cancels.add("model_b")

        async def next_frame():
            return await it.__anext__()

        step = asyncio.create_task(next_frame())
        await asyncio.sleep(0.7)  # a tick saw the flag; the stream awaits the worker
        awaiting = not step.done()
        step.cancel()  # the aborted fetch closes the stream mid-await
        try:
            await step
        except asyncio.CancelledError:
            pass
        worker_may_look.set()
        await asyncio.sleep(0.6)
        serving.end_turn()  # model_a's turn finishes
        await asyncio.sleep(0.6)
        return awaiting

    assert asyncio.run(run())  # the disconnect did land during the await
    assert events == []  # model_b was never started, model_a never stopped
    assert state["resident"] == "model_a"
    with routes_mod._load_cancels_lock:
        assert "model_b" not in routes_mod._load_cancels  # settled, not stranded
