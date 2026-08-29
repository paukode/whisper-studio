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
