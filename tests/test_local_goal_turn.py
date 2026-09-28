"""/goal on an on-device model, end to end through the real chat route.

The real local bridge, LocalAdapter, turn engine, completion gate and
on-device judge run; only llama-server is stubbed (tests/local_chat_stack.py),
and it answers the judge's non-streaming requests from a script. The judge is
the session's own resident model in Local and Hybrid mode alike, nothing is
sent to the cloud, and a plain on-device turn with no goal never reaches the
gate.
"""

import json

import boto3
import pytest

from server.chat import stream_slot
from server.chat.engine import midturn_inbox
from server.goals import store as goal_store
from tests.local_chat_stack import LOCAL_KEY, client, install_local_stack, post

LABEL = "Gemma 4 12B (Local)"


def _verdict(verdict, feedback="", confidence=0.9):
    return json.dumps({"verdict": verdict, "confidence": confidence, "feedback": feedback})


def _frames(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


def _goal_evals(frames):
    return [f["goal_eval"] for f in frames if "goal_eval" in f]


def _user_text(body: dict) -> str:
    return body["messages"][-1]["content"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()
    from server.goals import local_judge

    monkeypatch.setattr(local_judge, "_aux_note_logged", False)
    yield
    stream_slot.active_streams.clear()
    stream_slot.heartbeats.clear()


@pytest.fixture
def cloud_calls(monkeypatch):
    """Every way a judge could reach the cloud, recorded instead of run."""
    calls: list[str] = []

    def _client(self, service, *a, **k):
        calls.append(f"boto3:{service}")
        raise RuntimeError("no cloud client in this test")

    def _aux(task, *a, **k):
        calls.append(f"aux_one_shot:{task}")
        raise RuntimeError("no auxiliary call in this test")

    monkeypatch.setattr(boto3.session.Session, "client", _client)
    monkeypatch.setattr("server.infrastructure.auxiliary.aux_one_shot", _aux)
    return calls


def _goal(sid, text="write the summary"):
    midturn_inbox.drain(sid)
    goal_store.set_goal(sid, text)


@pytest.mark.parametrize("mode", ["local", "hybrid"])
@pytest.mark.parametrize("aux_key", [None, "haiku", "main", "local_other"])
def test_two_consecutive_goal_turns_are_judged_on_the_resident_model(
    monkeypatch, cloud_calls, caplog, mode, aux_key
):
    """Turn one ends on a confident blocker, the goal stays set; turn two is
    judged achieved and the goal ends. Both are judged by the session's own
    resident model whatever auxiliary_models.goal_evaluator says."""
    stack = install_local_stack(
        monkeypatch,
        mode=mode,
        replies=["I need the API key first.", "Here is the summary."],
        judge_replies=[
            _verdict("blocked", "needs an API key from the user"),
            _verdict("achieved", "the summary is in the reply"),
        ],
    )
    monkeypatch.setattr(
        "server.infrastructure.auxiliary._map",
        lambda config=None: {"goal_evaluator": aux_key} if aux_key else {},
    )
    sid = f"goal-two-turns-{mode}-{aux_key}"
    _goal(sid)
    http = client()

    with caplog.at_level("INFO", logger="whisper-studio"):
        first = _frames(post(http, sid, "write the summary").text)
    assert [e["verdict"] for e in _goal_evals(first)] == ["blocked"]
    assert goal_store.is_active(sid)

    second = _frames(post(http, sid, "the key is in .env").text)
    evals = _goal_evals(second)
    assert [e["verdict"] for e in evals] == ["achieved"]
    assert evals[0]["feedback"].startswith(f"Judged by {LABEL} on this Mac:")
    assert not goal_store.is_active(sid)

    # One judge request per turn, each on the session's model, and each sees
    # the reply it judges.
    assert len(stack.judge_requests) == 2
    assert all(r["model"] == "gemma" for r in stack.judge_requests)
    assert "I need the API key first." in _user_text(stack.judge_requests[0])
    assert "Here is the summary." in _user_text(stack.judge_requests[1])
    assert cloud_calls == []
    assert stack.turns["started"] == stack.turns["ended"]
    noted = [r for r in caplog.records if "applies to cloud-model sessions only" in r.message]
    assert len(noted) == (1 if aux_key else 0)


def test_not_achieved_continues_the_turn_on_device(monkeypatch, cloud_calls):
    stack = install_local_stack(
        monkeypatch,
        replies=["First try.", "Done now."],
        judge_replies=[
            _verdict("not_achieved", "the summary is missing its conclusion"),
            _verdict("achieved", "done"),
        ],
    )
    sid = "goal-continue"
    _goal(sid)
    frames = _frames(post(client(), sid, "write the summary").text)

    # The status names the judge, right before each verdict.
    for i, f in enumerate(frames):
        if "goal_eval" in f:
            assert frames[i - 1] == {"status": f"Checking the goal on {LABEL}..."}
    evals = _goal_evals(frames)
    assert [e["verdict"] for e in evals] == ["not_achieved", "achieved"]
    # The user is told the model judged its own work.
    assert all(e["feedback"].startswith(f"Judged by {LABEL} on this Mac:") for e in evals)

    # The judge request is constrained, deterministic and sees the reply.
    judge = stack.judge_requests[0]
    assert judge["response_format"]["type"] == "json_schema"
    schema = judge["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["achieved", "not_achieved", "blocked"]
    assert judge["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in judge and judge["temperature"] == 0
    assert "First try." in _user_text(judge)

    # The continuation: same tools, the same prefix plus the gated reply, and
    # the feedback persisted as the next user message.
    assert len(stack.requests) == 2
    one, two = stack.requests
    assert two.get("tools") == one.get("tools")
    assert two["messages"][: len(one["messages"])] == one["messages"]
    assert two["messages"][len(one["messages"])]["role"] == "assistant"
    assert "First try." in json.dumps(two["messages"][len(one["messages"])])
    assert "[completion gate]" in json.dumps(two["messages"][-1])
    assert "missing its conclusion" in json.dumps(two["messages"][-1])
    # The disclosure is for the user; the model gets the feedback alone.
    assert "Judged by" not in json.dumps(two["messages"][-1])

    assert not goal_store.is_active(sid)
    assert cloud_calls == []
    assert stack.turns["started"] == stack.turns["ended"]


def test_plain_local_turn_never_reaches_the_gate(monkeypatch, cloud_calls):
    stack = install_local_stack(monkeypatch)
    gate_calls: list = []

    async def _gate(ctx):
        gate_calls.append(ctx)
        raise AssertionError("the gate ran on a plain on-device turn")

    monkeypatch.setattr("server.goals.gate.run_completion_gate", _gate)
    sid = "goal-none"
    midturn_inbox.drain(sid)
    frames = _frames(post(client(), sid, "hey!").text)
    assert gate_calls == []
    assert stack.judge_requests == []
    assert not _goal_evals(frames)
    assert not any("stop_hook_block" in f or "goal_cap_reached" in f for f in frames)
    assert not any("Checking the goal" in str(f.get("status", "")) for f in frames)
    assert cloud_calls == []


def test_unreadable_judge_ends_the_turn_as_not_checked(monkeypatch, cloud_calls):
    stack = install_local_stack(
        monkeypatch,
        replies=["Here it is."],
        judge_replies=["Looks good to me!", "I think it is done."],
    )
    sid = "goal-garbage"
    _goal(sid)
    resp = post(client(), sid, "write the summary")
    evals = _goal_evals(_frames(resp.text))
    assert [e["verdict"] for e in evals] == ["not_checked"]
    assert evals[0]["feedback"].startswith(f"{LABEL} could not check the goal:")
    assert "The goal stays set" in evals[0]["feedback"]
    assert resp.text.rstrip().endswith("data: [DONE]")
    assert len(stack.judge_requests) == 2  # one retry
    assert len(stack.requests) == 1  # no continuation
    state = goal_store.get_goal(sid)["state"]
    assert goal_store.is_active(sid) and state["last_verdict"] == "not_checked"
    assert state["consecutive_blocks"] == 0
    assert stack.turns["started"] == stack.turns["ended"]


def test_goal_cap_reached_is_emitted_when_the_cap_is_hit(monkeypatch, cloud_calls):
    from server.infrastructure import config as cfg

    real_get = cfg.get
    monkeypatch.setattr(
        cfg,
        "get",
        lambda key, default=None: 2
        if key == "goal_max_consecutive_blocks"
        else real_get(key, default),
    )
    stack = install_local_stack(
        monkeypatch,
        replies=["Attempt."],
        judge_replies=[_verdict("not_achieved", "keep going", 0.5)],
    )
    sid = "goal-cap"
    _goal(sid)
    frames = _frames(post(client(), sid, "write the summary").text)
    assert [e["verdict"] for e in _goal_evals(frames)] == ["not_achieved", "not_achieved"]
    caps = [f["goal_cap_reached"] for f in frames if "goal_cap_reached" in f]
    assert caps == [{"attempt": 2, "cap": 2, "source": "evaluator"}]
    assert len(stack.judge_requests) == 2  # never judged at the cap
    assert len(stack.requests) == 3
    assert goal_store.is_active(sid)
    assert stack.turns["started"] == stack.turns["ended"]


def test_the_session_model_key_reaches_the_judge(monkeypatch, cloud_calls):
    """The judge is keyed by the session's model, which is the one the
    resident-only call is asked for."""
    install_local_stack(monkeypatch, replies=["Done."], judge_replies=[_verdict("achieved", "ok")])
    from server.local import serving

    asked: list[str] = []
    real = serving.begin_resident_call

    def _begin(key):
        asked.append(key)
        return real(key)

    monkeypatch.setattr(serving, "begin_resident_call", _begin)
    sid = "goal-key"
    _goal(sid)
    post(client(), sid, "write the summary")
    assert asked == [LOCAL_KEY]


@pytest.mark.parametrize("goal_set", [True, False])
def test_a_goal_turn_on_its_last_round_says_the_goal_was_not_checked(
    monkeypatch, cloud_calls, goal_set
):
    """The gate cannot run on a turn's last round (a block would need another
    round). With a goal in play the turn still ends with a reason, recorded as
    not checked; without one nothing is said."""
    from server.chat.engine import policy

    monkeypatch.setattr(
        policy, "LOCAL_POLICY", policy.TurnPolicy(max_rounds=1, gate_requires_goal=True)
    )
    stack = install_local_stack(monkeypatch, replies=["Partial answer."], judge_replies=[])
    sid = f"goal-last-round-{goal_set}"
    midturn_inbox.drain(sid)
    if goal_set:
        _goal(sid)
    frames = _frames(post(client(), sid, "write the summary").text)
    evals = _goal_evals(frames)
    assert stack.judge_requests == []
    if not goal_set:
        assert evals == []
        return
    assert [e["verdict"] for e in evals] == ["not_checked"]
    assert "the turn used its last round" in evals[0]["feedback"]
    assert "The goal stays set" in evals[0]["feedback"]
    state = goal_store.get_goal(sid)["state"]
    assert goal_store.is_active(sid) and state["last_verdict"] == "not_checked"
    assert state["consecutive_blocks"] == 0
    assert cloud_calls == []
