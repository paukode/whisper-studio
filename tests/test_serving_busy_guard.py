"""The serving facade's busy-turn guard.

Root cause of "asked about my CV, got an empty answer": switching models in
the composer fired a load whose transition STOPPED the server that was
mid-stream answering the question (round 0 died with an empty connection
error). ensure_serving must now wait for live turns to finish before any
transition that would displace the resident server, honor should_abort while
waiting, and register a turn atomically via mark_busy.
"""

import threading
import time

import pytest

from server.local import llama_server, mlx_server, runtime, serving


@pytest.fixture(autouse=True)
def _reset_busy():
    # The counter is module state; a failed test must not poison the next.
    yield
    while serving.busy_turns() > 0:
        serving.end_turn()


def _stub_engines(monkeypatch, resident: str | None, stops: list[str]):
    """A fake llama engine serving `resident`; mlx idle. Unknown residency
    plumbing kept minimal: engine_of() maps every key to gguf by default."""
    monkeypatch.setattr(runtime, "LOCAL_MODELS", {"model_a": {}, "model_b": {}})
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda k: "/w")
    monkeypatch.setattr(llama_server, "resident_key", lambda: resident)
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: 4096)
    monkeypatch.setattr(llama_server, "base_url", lambda: "http://a")
    monkeypatch.setattr(llama_server, "is_running", lambda: resident is not None)
    monkeypatch.setattr(llama_server, "ensure_available", lambda: None)
    monkeypatch.setattr(llama_server, "stop", lambda: stops.append("llama"))
    monkeypatch.setattr(llama_server, "ensure_serving", lambda k, n=None: f"http://{k}")
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "is_running", lambda: False)
    monkeypatch.setattr(mlx_server, "stop", lambda: stops.append("mlx"))


def test_fast_path_marks_busy_for_resident_model(monkeypatch):
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    url = serving.ensure_serving("model_a", mark_busy=True)
    assert url == "http://a" and serving.busy_turns() == 1
    serving.end_turn()
    assert serving.busy_turns() == 0


def test_displacing_load_waits_for_live_turn(monkeypatch):
    """model_b's load must not touch model_a's server while a turn streams
    from it; it proceeds the moment the turn ends."""
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    serving.begin_turn()  # a live turn on model_a

    order: list[str] = []
    done = threading.Event()

    def _load_b():
        serving.ensure_serving("model_b")
        order.append("switched")
        done.set()

    t = threading.Thread(target=_load_b, daemon=True)
    t.start()
    time.sleep(0.6)  # well past a few poll ticks
    assert not done.is_set() and stops == []  # still waiting, nothing stopped
    order.append("turn-finished")
    serving.end_turn()
    assert done.wait(5), "load never proceeded after the turn ended"
    assert order == ["turn-finished", "switched"]


def test_same_model_turn_is_not_blocked_by_its_own_busy_flag(monkeypatch):
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    serving.begin_turn()
    # A second turn on the RESIDENT model takes the fast path instantly.
    url = serving.ensure_serving("model_a", mark_busy=True)
    assert url == "http://a" and serving.busy_turns() == 2
    serving.end_turn()
    serving.end_turn()


def test_should_abort_breaks_the_wait(monkeypatch):
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    serving.begin_turn()
    flag = {"cancel": False}
    err: list[str] = []

    def _load_b():
        try:
            serving.ensure_serving("model_b", should_abort=lambda: flag["cancel"])
        except RuntimeError as e:
            err.append(str(e))

    t = threading.Thread(target=_load_b, daemon=True)
    t.start()
    time.sleep(0.4)
    flag["cancel"] = True
    t.join(5)
    assert err and "cancelled" in err[0].lower()
    assert stops == []  # the live server was never touched
    serving.end_turn()


def test_wait_times_out_with_a_clear_error(monkeypatch):
    stops: list[str] = []
    _stub_engines(monkeypatch, resident="model_a", stops=stops)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 0)
    serving.begin_turn()
    with pytest.raises(RuntimeError, match="still answering"):
        serving.ensure_serving("model_b")
    serving.end_turn()


def _stub_slow_switch(monkeypatch, *, download_s=0.0, serve_s=0.0):
    """model_a resident and idle; loading model_b downloads for ``download_s``
    and then spends ``serve_s`` inside the engine start. Every stop and start
    records how many turns were live at that moment."""
    events: list[tuple[str, int]] = []
    state = {"resident": "model_a"}
    inside_start = threading.Event()

    def _download(k):
        if download_s:
            time.sleep(download_s)
        return "/w"

    def _serve(k, n=None):
        events.append((f"start:{k}", serving.busy_turns()))
        inside_start.set()
        time.sleep(serve_s)
        events.append((f"started:{k}", serving.busy_turns()))
        state["resident"] = k
        return f"http://{k}"

    monkeypatch.setattr(runtime, "LOCAL_MODELS", {"model_a": {}, "model_b": {}})
    monkeypatch.setattr(runtime, "ensure_downloaded", _download)
    monkeypatch.setattr(llama_server, "resident_key", lambda: state["resident"])
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: 4096)
    monkeypatch.setattr(llama_server, "base_url", lambda: f"http://{state['resident']}")
    monkeypatch.setattr(llama_server, "is_running", lambda: True)
    monkeypatch.setattr(llama_server, "ensure_available", lambda: None)
    monkeypatch.setattr(llama_server, "stop", lambda: events.append(("stop", serving.busy_turns())))
    monkeypatch.setattr(llama_server, "ensure_serving", _serve)
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "is_running", lambda: False)
    monkeypatch.setattr(mlx_server, "stop", lambda: events.append(("mlx-stop", 0)))
    return events, inside_start


def test_turn_that_starts_during_a_download_is_not_displaced(monkeypatch):
    """A turn on the resident model that begins while another model's weights
    download must hold that switch off until it ends: the busy check has to
    run after the download, right before anything is stopped."""
    events, _ = _stub_slow_switch(monkeypatch, download_s=0.6)
    switched = threading.Event()

    def _load_b():
        serving.ensure_serving("model_b")
        switched.set()

    t = threading.Thread(target=_load_b, daemon=True)
    t.start()
    time.sleep(0.2)  # model_b is downloading
    assert serving.ensure_serving("model_a", mark_busy=True) == "http://model_a"
    time.sleep(0.8)  # the download has finished by now
    assert not switched.is_set()
    assert [e for e in events if e[0].startswith("start")] == []
    serving.end_turn()  # the model_a turn finishes
    assert switched.wait(5)
    assert all(busy == 0 for _, busy in events)


def test_turn_arriving_mid_transition_waits_instead_of_registering(monkeypatch):
    """While a switch is stopping and restarting the server, a turn on the
    model that is about to go away must not register on it through the
    lock-free fast path; it queues behind the switch."""
    events, inside_start = _stub_slow_switch(monkeypatch, serve_s=0.5)
    t = threading.Thread(target=lambda: serving.ensure_serving("model_b"), daemon=True)
    t.start()
    assert inside_start.wait(5)  # the switch is inside the engine start
    got: list[str] = []
    turn = threading.Thread(
        target=lambda: got.append(serving.ensure_serving("model_a", mark_busy=True)),
        daemon=True,
    )
    turn.start()
    t.join(5)
    turn.join(5)
    started_b = next(busy for name, busy in events if name == "started:model_b")
    assert started_b == 0  # nothing registered while model_b was coming up
    assert got == ["http://model_a"] and serving.busy_turns() == 1
    serving.end_turn()
