"""``serving.stop_if_idle`` never stops a server a turn is about to use.

The load endpoint's cancel stops what that load started. It used to check
only the busy-turn count, so it could stop the server under a turn whose slow
path already held the URL but had not registered yet, and a turn could pick
up the server while the stop was still in progress. Either way the turn's
first round failed with a lost connection.

These drive the real ``serving.ensure_serving`` over a stubbed llama engine.
"""

import threading
import time

import pytest

from server.local import llama_server, mlx_server, runtime, serving


@pytest.fixture(autouse=True)
def _reset_busy():
    yield
    while serving.busy_turns() > 0:
        serving.end_turn()


def _stub(monkeypatch, *, resident=None, serve=None, stop=None):
    """One llama engine; ``serve(k)`` and ``stop()`` hooks run inside the
    engine calls. Every start and stop is recorded."""
    events: list[str] = []
    state = {"resident": resident}

    def _serve(k, n=None):
        if serve is not None:
            serve(k, state)
        if state["resident"] != k:
            events.append(f"start:{k}")
            state["resident"] = k
        return f"http://{k}"

    def _stop():
        events.append("stop")
        if stop is not None:
            stop()
        state["resident"] = None

    monkeypatch.setattr(runtime, "LOCAL_MODELS", {"model_a": {}})
    monkeypatch.setattr(runtime, "ensure_downloaded", lambda k: "/w")
    monkeypatch.setattr(llama_server, "resident_key", lambda: state["resident"])
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: 4096)
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


def test_a_load_may_stop_the_server_it_is_warming_and_nothing_else_may(monkeypatch):
    """The cancel's own load is the one claim that does not block the stop."""
    warming = threading.Event()
    release = threading.Event()

    def serve(k, state):
        state["resident"] = k  # llama-server records the key before its health wait
        warming.set()
        release.wait(5)
        if state["resident"] != k:
            raise RuntimeError("llama-server failed to become ready")

    events, state = _stub(monkeypatch, serve=serve)
    started = threading.Event()
    errors: list[str] = []

    def _load():
        try:
            serving.ensure_serving("model_a", started=started)
        except RuntimeError as e:
            errors.append(str(e))

    load = threading.Thread(target=_load, daemon=True)
    load.start()
    assert warming.wait(5) and started.is_set()

    by_other = serving.stop_if_idle("model_a")
    by_owner = serving.stop_if_idle("model_a", started)
    release.set()
    load.join(5)
    assert by_other is False  # someone else's warming load
    assert by_owner is True  # the load's own cancel
    assert events == ["stop"] and state["resident"] is None
    assert errors  # the killed warm-up failed instead of coming up


def test_no_stop_while_a_turn_registers_through_the_slow_path(monkeypatch):
    """A turn queued behind a load of its model finds the model served, holds
    its URL, and has not registered yet. The load's post-load cancel must not
    stop the server in that gap."""
    warming = threading.Event()
    release_load = threading.Event()
    turn_inside = threading.Event()
    release_turn = threading.Event()

    def serve(k, state):
        if state["resident"] == k:  # the turn's no-op start, after the load
            turn_inside.set()
            release_turn.wait(5)
        else:
            warming.set()
            release_load.wait(5)

    events, state = _stub(monkeypatch, serve=serve)
    started = threading.Event()
    load = threading.Thread(
        target=lambda: serving.ensure_serving("model_a", started=started), daemon=True
    )
    load.start()
    assert warming.wait(5)
    got: list[str] = []
    turn = threading.Thread(
        target=lambda: got.append(serving.ensure_serving("model_a", mark_busy=True)),
        daemon=True,
    )
    turn.start()
    time.sleep(0.2)  # the turn is queued on the transition lock
    release_load.set()
    load.join(5)
    assert turn_inside.wait(5)  # the turn claimed and holds model_a's URL

    stopped = serving.stop_if_idle("model_a", started)
    release_turn.set()
    turn.join(5)
    assert stopped is False
    assert got == ["http://model_a"] and serving.busy_turns() == 1
    assert events == ["start:model_a"] and state["resident"] == "model_a"


def test_a_turn_arriving_mid_stop_waits_for_it_and_starts_fresh(monkeypatch):
    """Between stop_if_idle's check and the engine clearing its state, the
    server still looks resident. A turn must not pick it up then."""
    stopping = threading.Event()
    release_stop = threading.Event()

    def stop():
        stopping.set()
        release_stop.wait(5)

    events, state = _stub(monkeypatch, resident="model_a", stop=stop)
    stopper = threading.Thread(target=lambda: serving.stop_if_idle("model_a"), daemon=True)
    stopper.start()
    assert stopping.wait(5)
    got: list[str] = []
    turn = threading.Thread(
        target=lambda: got.append(serving.ensure_serving("model_a", mark_busy=True)),
        daemon=True,
    )
    turn.start()
    time.sleep(0.3)  # the turn arrived while the stop was still running
    during_stop = list(got)
    release_stop.set()
    stopper.join(5)
    turn.join(5)
    assert during_stop == []  # it waited instead of taking the stopping server
    assert events == ["stop", "start:model_a"]  # a fresh server, not the stopped one
    assert got == ["http://model_a"] and serving.busy_turns() == 1
    assert state["resident"] == "model_a"
