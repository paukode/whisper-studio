"""The resident-only side call the on-device goal judge makes from inside a
live turn (serving.complete_on_resident).

It runs while the turn holds its own busy mark, so it must never enter
ensure_serving's wait (that would wait on the turn's own mark), never load or
displace a model, never cancel a pending release, and take and release exactly
one mark of its own. Every failure is a ResidentCallError with a reason, never
an empty string.
"""

import asyncio
import json
import time

import httpx
import pytest

from server.local import llama_server, mlx_server, runtime, serving

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _clean_serving(monkeypatch):
    monkeypatch.setattr(serving, "_busy_turns", 0)
    monkeypatch.setattr(serving, "_claims", [])
    monkeypatch.setattr(serving, "_stopping", 0)
    monkeypatch.setattr(serving, "_release_when_idle", None)
    monkeypatch.setattr(
        serving, "ensure_serving", lambda *a, **k: pytest.fail("ensure_serving was called")
    )
    yield


def _resident(monkeypatch, key="model_a", *, engine="gguf", thinking=True):
    """``key`` is resident on ``engine``; the other engine is idle."""
    monkeypatch.setattr(
        runtime,
        "LOCAL_MODELS",
        {
            "model_a": {"engine": engine, "supports_thinking": thinking},
            "model_b": {"engine": engine, "supports_thinking": thinking},
        },
    )
    target, other = (mlx_server, llama_server) if engine == "mlx" else (llama_server, mlx_server)
    monkeypatch.setattr(target, "resident_key", lambda: key)
    monkeypatch.setattr(target, "base_url", lambda: "http://resident.test")
    monkeypatch.setattr(target, "resident_n_ctx", lambda: 32768)
    monkeypatch.setattr(other, "resident_key", lambda: None)
    monkeypatch.setattr(other, "base_url", lambda: None)
    monkeypatch.setattr(other, "resident_n_ctx", lambda: None)


def _server(monkeypatch, respond):
    """Route httpx.AsyncClient to ``respond(body) -> httpx.Response``."""
    seen: list[dict] = []
    real = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        out = respond(body)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _answer(content="{}", finish="stop", **msg):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content, **msg}, "finish_reason": finish}]},
    )


def _call(key="model_a", **kw):
    kw.setdefault("max_tokens", 64)
    return asyncio.run(serving.complete_on_resident(key, "sys", "user", **kw))


def test_resident_call_during_own_turn_posts_without_waiting(monkeypatch):
    _resident(monkeypatch)
    seen = _server(monkeypatch, lambda body: _answer('{"verdict": "achieved"}'))
    serving.begin_turn()  # the live turn's own mark
    t0 = time.monotonic()
    out = _call()
    assert out == '{"verdict": "achieved"}'
    assert time.monotonic() - t0 < 0.5
    assert len(seen) == 1
    assert serving.busy_turns() == 1


def test_resident_call_refuses_another_key_at_once(monkeypatch):
    _resident(monkeypatch)
    seen = _server(monkeypatch, lambda body: _answer())
    serving.begin_turn()
    t0 = time.monotonic()
    with pytest.raises(serving.ResidentCallError, match="no longer loaded"):
        _call("model_b")
    assert time.monotonic() - t0 < 0.5
    assert seen == []
    assert serving.busy_turns() == 1


def test_refused_begin_leaves_the_busy_count_unchanged(monkeypatch):
    """A claimed transition refuses the begin. The refusal holds no mark, so
    it must not call end_turn: that would release the TURN's own mark and let
    a displacing load stop the server under the continuation rounds."""
    _resident(monkeypatch)
    seen = _server(monkeypatch, lambda body: _answer())
    ends: list[int] = []
    real_end = serving.end_turn
    monkeypatch.setattr(serving, "end_turn", lambda: (ends.append(1), real_end()))
    serving.begin_turn()
    serving._claims.append(object())  # a transition is claimed
    with pytest.raises(serving.ResidentCallError, match="model switch"):
        _call()
    assert serving.busy_turns() == 1
    assert ends == [] and seen == []


def test_resident_call_holds_one_mark_for_its_request(monkeypatch):
    _resident(monkeypatch)
    serving.begin_turn()
    before = serving.busy_turns()
    during: dict = {}

    def respond(body):
        during["busy"] = serving.busy_turns()
        during["stopped"] = serving.stop_if_idle("model_a")
        return _answer("ok")

    monkeypatch.setattr(serving, "stop", lambda: pytest.fail("stopped under the request"))
    _server(monkeypatch, respond)
    assert _call() == "ok"
    assert during == {"busy": before + 1, "stopped": False}
    assert serving.busy_turns() == before


def test_resident_call_releases_its_mark_when_the_model_went_away_mid_begin(monkeypatch):
    """The residency re-read after a successful begin fails: that begin is
    paired with exactly one end_turn."""
    _resident(monkeypatch)
    seen = _server(monkeypatch, lambda body: _answer())
    reads = iter(["http://resident.test", None])
    monkeypatch.setattr(serving, "_served_url", lambda engine, key, n_ctx: next(reads))
    serving.begin_turn()
    with pytest.raises(serving.ResidentCallError, match="no longer loaded"):
        _call()
    assert serving.busy_turns() == 1 and seen == []


def test_resident_call_keeps_a_pending_release(monkeypatch):
    """The user switched away from the model mid-turn: it is released when the
    last turn ends. A judge call on it in between must not cancel that."""
    _resident(monkeypatch)
    _server(monkeypatch, lambda body: _answer("ok"))
    serving.begin_turn()
    monkeypatch.setattr(serving, "_release_when_idle", "model_a")
    _call()
    assert serving._release_when_idle == "model_a"


def test_cancelling_the_call_releases_its_mark(monkeypatch):
    _resident(monkeypatch)
    serving.begin_turn()
    before = serving.busy_turns()
    started = asyncio.Event()

    async def hang(body):
        started.set()
        await asyncio.Event().wait()

    _server(monkeypatch, hang)

    async def go():
        task = asyncio.create_task(serving.complete_on_resident("model_a", "s", "u", max_tokens=8))
        await started.wait()
        assert serving.busy_turns() == before + 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert serving.busy_turns() == before


def test_llama_server_body_is_constrained_and_tool_free(monkeypatch):
    _resident(monkeypatch, engine="gguf", thinking=True)
    seen = _server(monkeypatch, lambda body: _answer("ok"))
    _call(json_schema=SCHEMA)
    body = seen[0]
    assert body["model"] == "model_a"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0 and body["stream"] is False
    assert "tools" not in body


def test_mlx_body_has_no_response_format(monkeypatch):
    _resident(monkeypatch, engine="mlx", thinking=False)
    seen = _server(monkeypatch, lambda body: _answer("ok"))
    _call(json_schema=SCHEMA)
    body = seen[0]
    assert body["model"] == mlx_server.WIRE_MODEL
    assert "response_format" not in body
    assert "chat_template_kwargs" not in body
    assert "tools" not in body


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(
                400, json={"error": {"code": 400, "message": "request exceeds the context size"}}
            ),
            "HTTP 400: request exceeds the context size",
        ),
        (_answer('{"verdict": "ach', finish="length"), "64-token budget"),
        (_answer("", reasoning_content="let me think"), "only reasoning"),
        (_answer("   "), "empty"),
        (httpx.Response(200, json={"choices": []}), "no answer"),
    ],
)
def test_failures_carry_a_reason(monkeypatch, response, reason):
    _resident(monkeypatch)
    _server(monkeypatch, lambda body: response)
    serving.begin_turn()
    with pytest.raises(serving.ResidentCallError) as exc:
        _call()
    assert reason in str(exc.value)
    assert serving.busy_turns() == 1


def test_a_timeout_is_a_reason(monkeypatch):
    _resident(monkeypatch)

    async def slow(body):
        await asyncio.sleep(5)
        return _answer("late")

    _server(monkeypatch, slow)
    with pytest.raises(serving.ResidentCallError, match="no answer within"):
        _call(timeout=0.2)
    assert serving.busy_turns() == 0


# ── the wait behind another live turn ──────────────────────────────────────


def _answers_after(seconds, content="ok"):
    async def late(body):
        await asyncio.sleep(seconds)
        return _answer(content)

    return late


def test_the_wait_behind_another_turn_is_not_the_answers_timeout(monkeypatch):
    """The server answers one request at a time, so a call made while another
    turn streams queues behind that turn's round. The wait is allowed for on
    top of ``timeout``, which bounds the answer."""
    _resident(monkeypatch)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 2.0)
    _server(monkeypatch, _answers_after(0.4))
    serving.begin_turn()  # the caller's own turn
    serving.begin_turn()  # another session's live turn
    assert _call(timeout=0.2, held_marks=1) == "ok"
    assert serving.busy_turns() == 2


def test_the_callers_own_turn_gets_no_wait_allowance(monkeypatch):
    _resident(monkeypatch)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 5.0)
    _server(monkeypatch, _answers_after(2.0))
    serving.begin_turn()  # only the caller's own turn
    t0 = time.monotonic()
    with pytest.raises(serving.ResidentCallError) as exc:
        _call(timeout=0.2, held_marks=1)
    assert time.monotonic() - t0 < 1.5
    assert "no answer within" in str(exc.value) and "another" not in str(exc.value)
    assert serving.busy_turns() == 1


def test_a_timeout_behind_another_turn_says_the_model_was_shared(monkeypatch):
    _resident(monkeypatch)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 0.2)
    _server(monkeypatch, _answers_after(5.0))
    serving.begin_turn()
    serving.begin_turn()
    with pytest.raises(serving.ResidentCallError, match="also answering another chat or agent"):
        _call(timeout=0.2, held_marks=1)
    assert serving.busy_turns() == 2
