"""Switching the picker away from an on-device model never kills a live answer.

POST /api/local-model/unload used to stop llama-server unconditionally, under
any chat turn or local agent still streaming from it in any session. Now it
frees the model at once only when nothing uses it, and otherwise once the last
turn streaming from it ends, unless something asks for the local server again
first.
"""

import asyncio
import time

import pytest

from server.chat import routes
from server.local import serving
from tests.test_serving_stop_if_idle import _stub


@pytest.fixture(autouse=True)
def _reset():
    yield
    while serving.busy_turns() > 0:
        serving.end_turn()
    serving._keep_resident()


def _wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_unload_frees_an_idle_model_at_once(monkeypatch):
    events, state = _stub(monkeypatch, resident="model_a")
    reply = asyncio.run(routes.local_model_unload())
    assert reply == {"unloaded": True}
    assert events == ["stop"] and state["resident"] is None


def test_unload_never_stops_a_model_a_turn_streams_from(monkeypatch):
    events, state = _stub(monkeypatch, resident="model_a")
    serving.begin_turn()
    reply = asyncio.run(routes.local_model_unload())
    assert reply["unloaded"] is False and reply["reason"]
    assert events == [] and state["resident"] == "model_a"
    # The answer ends: the model the user left is freed then.
    serving.end_turn()
    assert _wait_for(lambda: events == ["stop"])
    assert state["resident"] is None


def test_asking_for_the_model_again_cancels_the_pending_release(monkeypatch):
    events, state = _stub(monkeypatch, resident="model_a")
    serving.begin_turn()
    assert asyncio.run(routes.local_model_unload())["unloaded"] is False
    # The user switches back before the answer ends: the picker loads it.
    assert serving.ensure_serving("model_a") == "http://model_a"
    serving.end_turn()
    time.sleep(0.2)
    assert events == [] and state["resident"] == "model_a"
