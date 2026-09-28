"""The on-device goal judge and the gate phases around it.

An on-device session is judged by its own resident model through the
resident-only serving call; every way that can fail is a visible
``not_checked`` that keeps the goal. The tail is sized to the resident window.
The gate hands Stop hooks the ``local:...`` id, skips the file-producing
checks for a model with no tools, and lets the judge read the reply it gates.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server.goals import GateContext, GateDecision, Verdict, gate, local_judge
from server.goals import store as goal_store
from server.goals.evaluator import JUDGE_SYSTEM
from server.goals.tail import DEFAULT_CAP_CHARS, render_tail
from server.local import llama_server, mlx_server, runtime, serving

KEY = "local_gemma"
LABEL = "Gemma On Device"
ENTRY = {
    "id": "local:gemma-test",
    "label": LABEL,
    "engine": "gguf",
    "supports_thinking": True,
    "supports_tools": True,
    "ctx": 32768,
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _on_device(monkeypatch):
    """KEY is the resident llama-server model, with clean serving state."""
    monkeypatch.setattr(runtime, "LOCAL_MODELS", {KEY: ENTRY, "local_other": dict(ENTRY)})
    monkeypatch.setattr(serving, "_busy_turns", 0)
    monkeypatch.setattr(serving, "_claims", [])
    monkeypatch.setattr(serving, "_stopping", 0)
    monkeypatch.setattr(serving, "_release_when_idle", None)
    monkeypatch.setattr(
        serving, "ensure_serving", lambda *a, **k: pytest.fail("ensure_serving was called")
    )
    resident(monkeypatch, KEY, 32768)
    monkeypatch.setattr(mlx_server, "resident_key", lambda: None)
    monkeypatch.setattr(mlx_server, "resident_n_ctx", lambda: None)
    monkeypatch.setattr(mlx_server, "base_url", lambda: None)
    monkeypatch.setattr(local_judge, "_aux_note_logged", False)
    yield


def resident(monkeypatch, key, n_ctx):
    monkeypatch.setattr(llama_server, "resident_key", lambda: key)
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: n_ctx)
    monkeypatch.setattr(llama_server, "base_url", lambda: "http://resident.test")


def model_server(monkeypatch, respond):
    """httpx.AsyncClient answers with ``respond(body)``; returns the bodies."""
    seen: list[dict] = []
    real = httpx.AsyncClient

    async def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        out = respond(body)
        return await out if asyncio.iscoroutine(out) else out

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def answer(content, finish="stop"):
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    )


def verdict_json(verdict="achieved", feedback="done", confidence=0.9):
    return json.dumps({"verdict": verdict, "confidence": confidence, "feedback": feedback})


def judge(messages=None, goal="ship it", announce=None):
    return _run(
        local_judge.evaluate_on_device(
            goal, messages or [{"role": "user", "content": "go"}], model_key=KEY, announce=announce
        )
    )


# ── the verdict and its request ─────────────────────────────────────────────


def test_a_readable_verdict_discloses_the_self_judgment(monkeypatch):
    seen = model_server(monkeypatch, lambda body: answer(verdict_json("not_achieved", "add tests")))
    announced: list[str] = []
    v = judge(announce=announced.append)
    assert v.verdict == "not_achieved" and v.feedback == "add tests"
    assert v.shown_feedback == f"Judged by {LABEL} on this Mac: add tests"
    assert announced == [f"Checking the goal on {LABEL}..."]
    body = seen[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in body and body["temperature"] == 0
    assert serving.busy_turns() == 0


def _claim(monkeypatch):
    serving._claims.append(object())


def _gone(monkeypatch):
    resident(monkeypatch, "local_other", 32768)


def _small_window(monkeypatch):
    resident(monkeypatch, KEY, 2048)


def _slow(body):
    async def late():
        await asyncio.sleep(5)
        return answer(verdict_json())

    return late()


def _boom(monkeypatch):
    async def explode(*a, **k):
        raise ValueError("unexpected shape")

    monkeypatch.setattr(serving, "complete_on_resident", explode)


# (setup, server reply, expected requests, text in the reason)
_CAUSES = {
    "unreadable twice": (None, lambda b: answer("It looks done to me."), 2, "readable verdict"),
    "empty": (None, lambda b: answer(""), 1, "empty"),
    "cut off": (None, lambda b: answer('{"verdict": "ach', finish="length"), 1, "token budget"),
    "http error": (
        None,
        lambda b: httpx.Response(400, json={"error": {"message": "exceeds the context size"}}),
        1,
        "HTTP 400: exceeds the context size",
    ),
    "timeout": (None, _slow, 1, "no answer within"),
    "no longer resident": (_gone, lambda b: answer(verdict_json()), 0, "no longer loaded"),
    "transition claimed": (_claim, lambda b: answer(verdict_json()), 0, "model switch"),
    "window too small": (_small_window, lambda b: answer(verdict_json()), 0, "2,048 tokens"),
    "unexpected exception": (_boom, lambda b: answer(verdict_json()), 0, "ValueError"),
}


@pytest.mark.parametrize("cause", list(_CAUSES))
def test_every_failure_is_not_checked_with_the_reason(monkeypatch, cause):
    setup, respond, requests, reason = _CAUSES[cause]
    monkeypatch.setattr(local_judge, "TIMEOUT_S", 0.2)
    seen = model_server(monkeypatch, respond)
    if setup is not None:
        setup(monkeypatch)
    v = judge()
    assert v.is_not_checked and not v.is_achieved
    assert v.feedback.startswith(f"{LABEL} could not check the goal:")
    assert reason in v.feedback
    assert len(seen) == requests
    assert serving.busy_turns() == 0


def _late(seconds):
    async def late(body):
        await asyncio.sleep(seconds)
        return answer(verdict_json())

    return late


def test_a_check_behind_another_chat_waits_for_it_and_says_so(monkeypatch):
    """Another live turn on the one-slot server: the check may queue behind
    its round, so that wait does not count against the judge's timeout, and
    the status line says why the check may take longer."""
    monkeypatch.setattr(local_judge, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 2.0)
    model_server(monkeypatch, _late(0.4))
    serving.begin_turn()  # the judged turn
    serving.begin_turn()  # another chat on the same model
    announced: list[str] = []
    v = judge(announce=announced.append)
    assert v.is_achieved
    assert announced == [
        f"Checking the goal on {LABEL} (it is also answering another chat or agent, "
        "so this may wait)..."
    ]
    assert serving.busy_turns() == 2


def test_a_check_that_times_out_behind_another_chat_names_it(monkeypatch):
    monkeypatch.setattr(local_judge, "TIMEOUT_S", 0.1)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 0.1)
    model_server(monkeypatch, _late(5))
    serving.begin_turn()
    serving.begin_turn()
    v = judge()
    assert v.is_not_checked and "also answering another chat or agent" in v.feedback
    assert serving.busy_turns() == 2


def test_the_judged_turn_alone_is_not_a_reason_to_wait(monkeypatch):
    monkeypatch.setattr(local_judge, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(serving, "_BUSY_WAIT_TIMEOUT_S", 5.0)
    model_server(monkeypatch, _late(2))
    serving.begin_turn()  # the judged turn only
    announced: list[str] = []
    v = judge(announce=announced.append)
    assert v.is_not_checked and "no answer within" in v.feedback
    assert "another" not in v.feedback
    assert announced == [f"Checking the goal on {LABEL}..."]
    assert serving.busy_turns() == 1


def test_a_failure_announces_nothing_when_no_request_is_sent(monkeypatch):
    resident(monkeypatch, KEY, 2048)
    announced: list[str] = []
    v = judge(announce=announced.append)
    assert v.is_not_checked and announced == []


# ── the context window ──────────────────────────────────────────────────────


@pytest.mark.parametrize("n_ctx", [4096, 8192, 32768])
def test_the_judge_prompt_fits_the_resident_window(monkeypatch, n_ctx):
    resident(monkeypatch, KEY, n_ctx)
    seen = model_server(monkeypatch, lambda body: answer("prose, not a verdict"))
    huge = [{"role": "user", "content": "x" * 100_000}] + [
        {"role": "assistant", "content": f"step {i} " + "y" * 5_000} for i in range(20)
    ]
    goal = "make the whole suite green"
    judge(huge, goal=goal)
    budget = (n_ctx - local_judge.MAX_TOKENS - local_judge._SLACK_TOKENS) * 3
    for body in seen:  # the retry included
        sent = sum(len(m["content"]) for m in body["messages"])
        assert sent <= budget
    if n_ctx == 32768:
        user = seen[0]["messages"][1]["content"]
        assert render_tail(huge) in user  # the default cap is what fits here
    assert local_judge.tail_budget_chars(n_ctx, goal) <= DEFAULT_CAP_CHARS


def test_the_window_falls_back_to_the_registry_size(monkeypatch):
    monkeypatch.setattr(llama_server, "resident_n_ctx", lambda: None)
    monkeypatch.setitem(runtime.LOCAL_MODELS, KEY, {**ENTRY, "ctx": 2048})
    v = judge()
    assert v.is_not_checked and "2,048 tokens" in v.feedback


# ── which model judges ──────────────────────────────────────────────────────


@pytest.mark.parametrize("configured", [None, "haiku", "main", "local_other"])
def test_an_on_device_session_is_judged_by_its_own_model(monkeypatch, caplog, configured):
    monkeypatch.setattr(
        "server.infrastructure.auxiliary._map",
        lambda config=None: {"goal_evaluator": configured} if configured else {},
    )

    def no_cloud(*a, **k):
        raise AssertionError("the on-device judge reached for the auxiliary router")

    monkeypatch.setattr("server.infrastructure.auxiliary.aux_one_shot", no_cloud)
    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", no_cloud)
    asked: list[str] = []
    real_begin = serving.begin_resident_call
    monkeypatch.setattr(
        serving, "begin_resident_call", lambda k: (asked.append(k), real_begin(k))[1]
    )
    model_server(monkeypatch, lambda body: answer(verdict_json()))
    with caplog.at_level("INFO", logger="whisper-studio"):
        v = judge()
        judge()
    assert v.is_achieved and asked == [KEY, KEY]
    noted = [r for r in caplog.records if "applies to cloud-model sessions only" in r.message]
    assert len(noted) == (1 if configured else 0)


def test_a_cloud_goal_evaluator_of_main_is_the_session_model(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(
        "server.infrastructure.auxiliary._map", lambda config=None: {"goal_evaluator": "main"}
    )
    monkeypatch.setattr("server.infrastructure.auxiliary.aux_refusal", lambda *a, **k: None)
    seen: dict = {}

    def aux(task, system, user, *, max_tokens, main_model_key=None, **kw):
        seen["key"] = main_model_key
        return verdict_json()

    monkeypatch.setattr("server.infrastructure.auxiliary.aux_one_shot", aux)
    announced: list[str] = []
    v = evaluator.evaluate("g", [], main_model_key="opus5.0", announce=announced.append)
    assert v.is_achieved and seen["key"] == "opus5.0"
    assert announced and announced[0].startswith("Checking the goal on ")


def test_a_refused_cloud_judge_announces_nothing(monkeypatch):
    from server.goals import evaluator

    monkeypatch.setattr(
        "server.infrastructure.auxiliary.aux_refusal", lambda *a, **k: "refused here."
    )
    announced: list[str] = []
    v = evaluator.evaluate("g", [], announce=announced.append)
    assert v.is_not_checked and v.feedback == "refused here." and announced == []


# ── the gate around the judge ───────────────────────────────────────────────


def _session(sid, goal="ship it"):
    goal_store.set_goal(sid, goal)
    return sid


def _local_ctx(sid, **kw):
    return GateContext(
        session_id=sid,
        goal=kw.pop("goal", "ship it"),
        provider="local",
        model_id=KEY,
        model_key=KEY,
        **kw,
    )


def test_the_judge_reads_the_reply_it_gates(monkeypatch):
    seen = model_server(monkeypatch, lambda body: answer(verdict_json()))
    sid = _session("gate-final-reply")
    ctx = _local_ctx(
        sid,
        messages=[{"role": "user", "content": "write the summary"}],
        final_reply=[{"type": "text", "text": "THE FINAL SUMMARY"}],
    )
    d = _run(gate.run_completion_gate(ctx))
    assert d.goal_achieved
    assert "THE FINAL SUMMARY" in seen[0]["messages"][1]["content"]
    assert len(ctx.messages) == 1  # a view: nothing is appended to the caller's list


def test_a_cloud_judge_reads_the_reply_it_gates(monkeypatch):
    seen: dict = {}

    def fake_evaluate(goal, messages, *, main_model_key="", announce=None, session_id=""):
        seen["messages"] = messages
        seen["key"] = main_model_key
        return Verdict("achieved", "ok", 0.9)

    monkeypatch.setattr("server.goals.evaluator.evaluate", fake_evaluate)
    sid = _session("gate-cloud-final-reply")
    ctx = GateContext(
        session_id=sid,
        goal="ship it",
        model_key="opus5.0",
        messages=[{"role": "user", "content": "go"}],
        final_reply=[{"type": "text", "text": "cloud answer"}],
    )
    _run(gate.run_completion_gate(ctx))
    assert seen["messages"][-1] == {"role": "assistant", "content": ctx.final_reply}
    assert seen["key"] == "opus5.0"


def test_not_checked_on_device_keeps_the_goal_and_is_not_a_block(monkeypatch):
    model_server(monkeypatch, lambda body: answer(""))
    sid = _session("gate-not-checked")
    d = _run(gate.run_completion_gate(_local_ctx(sid, attempt=2)))
    assert not d.block and not d.goal_achieved
    frame = d.frame["goal_eval"]
    assert frame["verdict"] == "not_checked" and frame["attempt"] == 2
    assert frame["feedback"].endswith(
        "The goal stays set and is checked again when the next turn ends."
    )
    state = goal_store.get_goal(sid)["state"]
    assert goal_store.is_active(sid)
    assert state["last_verdict"] == "not_checked" and state["consecutive_blocks"] == 0


@pytest.mark.parametrize(
    ("provider", "expected"), [("local", ENTRY["id"]), ("anthropic", "cloud-id")]
)
def test_stop_hooks_get_the_chat_models_id(monkeypatch, provider, expected):
    from server.hooks.schema import HookOutcome

    seen: dict = {}

    async def fake_stop(session_id, workspace, *, stop_hook_active=False, model_id=""):
        seen["model_id"] = model_id
        return HookOutcome(decision="deny", reason="not yet")

    monkeypatch.setattr("server.hooks.check_stop_hooks", fake_stop)
    model_id = KEY if provider == "local" else "cloud-id"
    ctx = GateContext(session_id="s", provider=provider, model_id=model_id, model_key=KEY)
    d = _run(gate.run_completion_gate(ctx))
    assert d.source == "stop_hook" and seen["model_id"] == expected


def test_a_model_with_no_tools_is_not_asked_for_files(monkeypatch, tmp_path):
    """Phases 1.5 and 1.6 both ask for a tool action; a model with no tools
    cannot take it, so they stay quiet for it and the judge decides."""
    monkeypatch.setattr(gate, "_flag_on", lambda n, d=True: True)
    model_server(monkeypatch, lambda body: answer(verdict_json("achieved", "done")))
    claimed = [
        {"role": "user", "content": "write the report and save it to Downloads as report.md"},
        {"role": "assistant", "content": f"Saved to: `{tmp_path}/report.md`"},
    ]
    sid = _session("gate-no-tools")
    with_tools = _run(gate.run_completion_gate(_local_ctx(sid, messages=claimed)))
    assert with_tools.block and with_tools.source in ("deliverable", "requested_file")
    no_tools = _run(
        gate.run_completion_gate(_local_ctx(sid, messages=claimed, tools_enabled=False))
    )
    assert not no_tools.block and no_tools.goal_achieved


def test_a_requested_file_is_not_asked_of_a_model_with_no_tools(monkeypatch):
    monkeypatch.setattr(gate, "_flag_on", lambda n, d=True: n != "deliverable_check")
    model_server(monkeypatch, lambda body: answer(verdict_json("achieved", "done")))
    asked = [
        {"role": "user", "content": "draw the flow and save it to Downloads as flow.svg"},
        {"role": "assistant", "content": "Here is the flow as text: A then B."},
    ]
    sid = _session("gate-no-tools-requested")
    with_tools = _run(gate.run_completion_gate(_local_ctx(sid, messages=asked)))
    assert with_tools.block and with_tools.source == "requested_file"
    no_tools = _run(gate.run_completion_gate(_local_ctx(sid, messages=asked, tools_enabled=False)))
    assert not no_tools.block


# ── the status line while the judge runs ────────────────────────────────────


def test_the_status_streams_before_the_decision(monkeypatch):
    model_server(monkeypatch, lambda body: answer(verdict_json()))
    sid = _session("gate-progress")

    async def go():
        return [item async for item in gate.run_gate_with_progress(_local_ctx(sid))]

    items = _run(go())
    assert items[0] == {"status": f"Checking the goal on {LABEL}..."}
    assert isinstance(items[-1], GateDecision) and items[-1].goal_achieved


def test_no_status_when_the_judge_does_not_run(monkeypatch):
    sid = "gate-progress-no-goal"

    async def go():
        return [item async for item in gate.run_gate_with_progress(_local_ctx(sid, goal=""))]

    items = _run(go())
    assert len(items) == 1 and isinstance(items[0], GateDecision)


def test_closing_the_stream_cancels_the_judge_and_releases_its_mark(monkeypatch):
    started = asyncio.Event()

    async def hang(body):
        started.set()
        await asyncio.Event().wait()

    model_server(monkeypatch, hang)
    sid = _session("gate-progress-cancel")

    async def go():
        agen = gate.run_gate_with_progress(_local_ctx(sid))
        first = await agen.__anext__()
        await started.wait()
        assert serving.busy_turns() == 1
        await agen.aclose()
        for _ in range(5):
            await asyncio.sleep(0)
        return first

    assert _run(go())["status"].startswith("Checking the goal on")
    assert serving.busy_turns() == 0
    assert goal_store.is_active(sid)


def test_the_judge_system_prompt_is_the_shared_one(monkeypatch):
    seen = model_server(monkeypatch, lambda body: answer(verdict_json()))
    judge()
    assert seen[0]["messages"][0] == {"role": "system", "content": JUDGE_SYSTEM}
