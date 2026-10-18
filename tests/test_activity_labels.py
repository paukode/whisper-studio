"""Activity captions: the auxiliary model names each round's tool calls.

A chat turn's tool round gets a short model-written caption, sent as an
activity_label frame keyed by the round's first labelled call. Whenever the
model cannot give one (Local mode, the flag off, a refusal, an error, a
timeout) no frame is sent, the browser keeps the deterministic label, and the
turn ends with its [DONE] as before. No live model call: aux_one_shot is
faked where the labeller reads it.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from server.chat.engine import activity_labels as AL
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.costs.sources import SOURCE_LABELS, require_source
from server.infrastructure.auxiliary import TASK_SOURCES

DONE = "data: [DONE]\n\n"


def read(tool_id: str, path: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": "ws_read_file", "input": {"path": path}}


def call(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


class _Adapter:
    """Each positional list is one round of tool calls. The round after them
    answers in text deltas: "Done. " at once, then one more delta after each
    of ``gaps`` seconds ("All good." last). ``seen`` keeps the message list
    each round was sent, and ``started`` the time.monotonic() it began."""

    provider = "test"

    def __init__(self, *tool_rounds: list[dict], gaps: tuple[float, ...] = (0.0,)):
        self.tool_rounds = tool_rounds
        self.gaps = gaps
        self.seen: list[list[dict]] = []
        self.started: list[float] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.seen.append(list(messages))
        self.started.append(time.monotonic())
        if round_num < len(self.tool_rounds):
            content = list(self.tool_rounds[round_num])
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        later = [f"Step {i}. " for i in range(1, len(self.gaps))] + ["All good."]
        yield TextDelta(text="Done. ")
        for gap, text in zip(self.gaps, later, strict=True):
            if gap:
                await asyncio.sleep(gap)
            yield TextDelta(text=text)
        content = [{"type": "text", "text": "Done. " + "".join(later)}]
        yield RoundResult(stop_reason="end_turn", content=content, usage=Usage())


def _tool_pipeline(monkeypatch, outputs: dict | None = None, *, pause_on: tuple[int, ...] = ()):
    """The batch returns real ToolStates; the gate passes each output on, or
    ``outputs[id]`` in its place (an inline-approved write's own result). The
    tool rounds whose index is in ``pause_on`` stop for an approval."""
    import server.tool_executor as TE

    outputs = outputs or {}
    rounds = iter(range(100))

    async def fake_batch(tool_uses, **kw):
        return [
            TE.ToolState(tool_use=tu, status="completed", output=f"output of {tu['id']}")
            for tu in tool_uses
        ]

    async def fake_process(states, budget_fn, **kw):
        results = [
            {
                "type": "tool_result",
                "tool_use_id": s.tool_id,
                "content": outputs.get(s.tool_id, s.output),
            }
            for s in states
        ]
        return (results, [], next(rounds) in pause_on, False)

    monkeypatch.setattr(TE, "execute_tool_batch", fake_batch)
    monkeypatch.setattr(TE, "process_tool_results", fake_process)


def _ctx(adapter, *, cost_source: str = "chat") -> TurnContext:
    return TurnContext(
        cost_source=cost_source,
        session_id="labels",
        model_key="opus5.0",
        model_id="m",
        messages=[{"role": "user", "content": "check the config"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=4, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )


def _run(ctx: TurnContext) -> tuple[list[str], float]:
    """The turn's chunks and the seconds until its [DONE]. The loop is closed
    without waiting for worker threads, so a caption the cap abandoned does
    not hold the test."""
    loop = asyncio.new_event_loop()
    started = time.monotonic()
    done_at: list[float] = []

    async def go():
        chunks = []
        async for c in run_turn(ctx):
            if c == DONE:
                done_at.append(time.monotonic() - started)
            chunks.append(c)
        return chunks

    try:
        return loop.run_until_complete(go()), done_at[0]
    finally:
        loop.close()


def _labels(chunks: list[str]) -> list[dict]:
    return [
        json.loads(c[len("data: ") :])["activity_label"]
        for c in chunks
        if c.startswith("data: {") and '"activity_label"' in c
    ]


class _Recorder:
    """aux_one_shot stand-in: records each call and answers ``reply``."""

    def __init__(self, reply="Read the app config"):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(
        self,
        task,
        system,
        user,
        *,
        max_tokens,
        main_model_key=None,
        session_id="",
        get_bedrock_client=None,
    ):
        self.calls.append(
            {
                "task": task,
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "main_model_key": main_model_key,
                "session_id": session_id,
                "get_bedrock_client": get_bedrock_client,
                "thread": threading.current_thread().name,
            }
        )
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


@pytest.fixture
def model(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(AL, "aux_one_shot", fake)
    return fake


@pytest.fixture
def config(monkeypatch):
    """Pin keys on the NORMALIZED config every lazy load_config() returns."""
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(**extra) -> dict:
        cfg = {**real, **extra}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)
        return cfg

    return _set


# Delivery


def test_a_round_of_reads_gets_its_cleaned_caption_before_done(monkeypatch, model):
    model.reply = '"Read the app config."'
    _tool_pipeline(monkeypatch)
    adapter = _Adapter([read("tu_a", "src/config.py"), read("tu_b", "src/app.py")])
    chunks, _ = _run(_ctx(adapter))

    assert _labels(chunks) == [
        {"tool_use_id": "tu_a", "tool_use_ids": ["tu_a", "tu_b"], "label": "Read the app config"}
    ]
    label_at = next(i for i, c in enumerate(chunks) if '"activity_label"' in c)
    assert label_at < chunks.index(DONE) and chunks[-1] == DONE
    (sent,) = model.calls
    assert (sent["task"], sent["max_tokens"]) == ("activity_label", 24)
    assert (sent["main_model_key"], sent["session_id"]) == ("opus5.0", "labels")
    # On the captions' own threads and client, not the loop's default pool
    # and the shared chat client.
    assert sent["thread"].startswith("activity-label")
    assert sent["get_bedrock_client"] is AL._caption_client
    assert sent["system"] == AL.SYSTEM and "\u2014" not in AL.SYSTEM
    assert sent["user"].splitlines() == [
        "ws_read_file src/config.py: output of tu_a",
        "ws_read_file src/app.py: output of tu_b",
    ]


def test_a_caption_that_lands_while_the_next_round_streams_goes_out_in_place(monkeypatch, model):
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")], gaps=(0.3,))))

    texts = [i for i, c in enumerate(chunks) if c.startswith('data: {"text"')]
    label_at = next(i for i, c in enumerate(chunks) if '"activity_label"' in c)
    # Before the answer's second delta, 0.3 s later: sent as it landed, not
    # held to the end of the turn.
    assert label_at < texts[1]


def test_agent_task_and_card_calls_are_left_out_and_the_key_is_the_first_labelled_call(
    monkeypatch, model
):
    _tool_pipeline(monkeypatch)
    tools = [
        call("tu_spawn", "spawn_agent", task="research"),
        read("tu_read", "README.md"),
        call("tu_task", "task_create", title="x"),
        call("tu_team", "team_create", team_name="t"),
        call("tu_wf", "workflow_run", script="s"),
        call("tu_ci", "ci_watch", branch="main"),
        call("tu_grep", "ws_grep", pattern="TODO"),
    ]
    chunks, _ = _run(_ctx(_Adapter(tools)))

    (frame,) = _labels(chunks)
    assert (frame["tool_use_id"], frame["tool_use_ids"]) == ("tu_read", ["tu_read", "tu_grep"])
    assert model.calls[0]["user"].splitlines() == [
        "ws_read_file README.md: output of tu_read",
        "ws_grep TODO: output of tu_grep",
    ]


def test_a_round_of_cards_only_gets_no_caption(monkeypatch, model):
    _tool_pipeline(monkeypatch)
    tools = [call("tu_spawn", "spawn_agent", task="research"), call("tu_task", "task_list")]
    chunks, _ = _run(_ctx(_Adapter(tools)))
    assert _labels(chunks) == [] and model.calls == [] and chunks[-1] == DONE


def test_the_caption_reads_what_the_gate_passed_and_names_a_failure(monkeypatch, model):
    # An inline-approved write: the state still holds the approval sentinel,
    # the model received the write's own result.
    long_output = "x" * 500
    _tool_pipeline(
        monkeypatch,
        {
            "tu_edit": "Applied 1 edit to app.py",
            "tu_run": "Error: no such file",
            "tu_big": long_output,
        },
    )
    tools = [
        call("tu_edit", "ws_edit_file", path="app.py"),
        call("tu_run", "ws_run_command", command="npm  test\n--watch"),
        read("tu_big", "big.log"),
    ]
    _run(_ctx(_Adapter(tools)))
    edit, run, big = model.calls[0]["user"].splitlines()
    assert edit == "ws_edit_file app.py: Applied 1 edit to app.py"
    assert run == "ws_run_command npm test --watch (failed): Error: no such file"
    assert big == "ws_read_file big.log: " + "x" * AL.OUTPUT_HEAD_CHARS


def test_a_paused_round_starts_no_caption(monkeypatch, model):
    _tool_pipeline(monkeypatch, pause_on=(0,))
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    assert model.calls == [] and _labels(chunks) == [] and chunks[-1] == DONE


def _payload(tool_id: str) -> dict:
    return {"tool_use_id": tool_id, "tool_use_ids": [tool_id], "label": f"caption of {tool_id}"}


def test_captions_leave_in_round_order_when_a_later_one_lands_first(monkeypatch):
    """Round 1's caption takes 0.6 s, round 2's 0.05 s. The answer's second
    delta (0.2 s) comes when only round 2's has landed: it waits there, so
    the group that spans both rounds ends with round 2's caption."""
    delays = iter([0.6, 0.05])

    async def caption(tool_id: str, delay: float) -> dict:
        await asyncio.sleep(delay)
        return _payload(tool_id)

    def fake_start(ctx, states, tool_results):
        return asyncio.create_task(caption(states[0].tool_id, next(delays)))

    monkeypatch.setattr(AL, "start_label_task", fake_start)
    _tool_pipeline(monkeypatch)
    adapter = _Adapter([read("tu_1", "a.py")], [read("tu_2", "b.py")], gaps=(0.2, 0.6))
    chunks, _ = _run(_ctx(adapter))

    assert [frame["tool_use_id"] for frame in _labels(chunks)] == ["tu_1", "tu_2"]
    texts = [i for i, c in enumerate(chunks) if c.startswith('data: {"text"')]
    label_at = [i for i, c in enumerate(chunks) if '"activity_label"' in c]
    # Neither left at the 0.2 s delta; both left together at the last one.
    assert texts[1] < label_at[0] and label_at[1] < texts[2]


def test_ready_holds_a_caption_behind_an_earlier_one_and_a_pause_drops_the_earlier():
    async def go():
        async def landed(tool_id: str, delay: float) -> dict:
            await asyncio.sleep(delay)
            return _payload(tool_id)

        pending = AL.PendingLabels()
        slow = asyncio.create_task(landed("tu_1", 5))
        fast = asyncio.create_task(landed("tu_2", 0))
        pending.add(slow)
        pending.add(fast)
        await asyncio.sleep(0.01)
        assert fast.done() and pending.ready() == []
        frames = pending.flush()
        await asyncio.sleep(0)
        assert slow.cancelled()
        return frames

    loop = asyncio.new_event_loop()
    try:
        frames = loop.run_until_complete(go())
    finally:
        loop.close()
    assert _labels(frames) == [_payload("tu_2")]


async def _drained(
    pending: AL.PendingLabels, timeout: float | None = None
) -> list[tuple[str, float]]:
    """Each frame drain() yields, with the seconds since the drain began."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    return [(frame, loop.time() - started) async for frame in pending.drain(timeout)]


def test_the_end_of_turn_drain_yields_each_caption_as_it_lands():
    async def go():
        async def landed(tool_id: str, delay: float) -> dict:
            await asyncio.sleep(delay)
            return _payload(tool_id)

        pending = AL.PendingLabels()
        pending.add(asyncio.create_task(landed("tu_1", 0.05)))
        pending.add(asyncio.create_task(landed("tu_2", 0.4)))
        return await _drained(pending)

    loop = asyncio.new_event_loop()
    try:
        (first, first_at), (second, second_at) = loop.run_until_complete(go())
    finally:
        loop.close()
    assert _labels([first, second]) == [_payload("tu_1"), _payload("tu_2")]
    # Round 1's caption left as it landed, not with round 2's.
    assert first_at < 0.3 <= second_at


def test_the_drain_gives_up_on_a_hung_caption_and_still_sends_the_ones_behind_it():
    async def go():
        async def landed(tool_id: str, delay: float) -> dict:
            await asyncio.sleep(delay)
            return _payload(tool_id)

        pending = AL.PendingLabels()
        hung = asyncio.create_task(landed("tu_1", 5))
        pending.add(hung)
        pending.add(asyncio.create_task(landed("tu_2", 0)))
        frames = await _drained(pending, timeout=0.1)
        await asyncio.sleep(0)
        return frames, hung

    loop = asyncio.new_event_loop()
    try:
        frames, hung = loop.run_until_complete(go())
    finally:
        loop.close()
    assert _labels([frame for frame, _ in frames]) == [_payload("tu_2")]
    assert hung.cancelled() and frames[0][1] >= 0.1


def test_a_message_sent_during_the_end_of_turn_wait_is_answered_in_the_same_turn(monkeypatch):
    """The round's caption takes 1 s, so the answer ends while it still runs.
    A composer message sent 0.5 s in reaches the turn: the inbox stays open
    through the captions' wait, and the last look after it gives the message
    one more round, so it is neither refused nor left unanswered."""
    from server.chat.engine import midturn_inbox

    async def slow_caption(tool_id: str) -> dict:
        await asyncio.sleep(1.0)
        return _payload(tool_id)

    monkeypatch.setattr(
        AL,
        "start_label_task",
        lambda ctx, states, results: asyncio.create_task(slow_caption("tu_a")),
    )
    _tool_pipeline(monkeypatch)
    adapter = _Adapter([read("tu_a", "a.py")])
    ctx = _ctx(adapter)
    ctx.midturn_inbox = True
    midturn_inbox.clear(ctx.session_id)
    accepted: list[bool] = []

    async def send_during_the_wait():
        await asyncio.sleep(0.5)
        accepted.append(midturn_inbox.push(ctx.session_id, "also check b.py"))

    async def go():
        sender = asyncio.create_task(send_during_the_wait())
        chunks = [c async for c in run_turn(ctx)]
        await sender
        return chunks

    loop = asyncio.new_event_loop()
    try:
        chunks = loop.run_until_complete(go())
        # The turn's inbox closed at its end: a later send starts a new turn.
        assert midturn_inbox.push(ctx.session_id, "and this?") is False
    finally:
        midturn_inbox.clear(ctx.session_id)
        loop.close()

    assert accepted == [True]
    # One more round of the same turn read the message and answered it.
    assert len(adapter.seen) == 3
    assert "also check b.py" in json.dumps(adapter.seen[2])
    assert chunks.count(DONE) == 1 and chunks[-1] == DONE
    assert midturn_inbox.UNANSWERED not in "".join(chunks)
    # The caption went out in the wait, before the answer to the message.
    texts = [i for i, c in enumerate(chunks) if c.startswith('data: {"text"')]
    label_at = next(i for i, c in enumerate(chunks) if '"activity_label"' in c)
    assert _labels(chunks) == [_payload("tu_a")]
    assert texts[1] < label_at < texts[-1]


def test_a_message_already_pending_at_the_answer_skips_the_caption_wait(monkeypatch):
    """A composer message lands while the answer still streams, and the
    round's caption takes 1.5 s. The message's round starts as soon as the
    answer ends, not after the caption: the turn has a round left, so the
    wait would only delay it. The caption still goes out once, in that
    round or the wait after it."""
    from server.chat.engine import midturn_inbox

    async def slow_caption(tool_id: str) -> dict:
        await asyncio.sleep(1.5)
        return _payload(tool_id)

    monkeypatch.setattr(
        AL,
        "start_label_task",
        lambda ctx, states, results: asyncio.create_task(slow_caption("tu_a")),
    )
    _tool_pipeline(monkeypatch)
    # The answer streams for 0.3 s; the message arrives 0.1 s in.
    adapter = _Adapter([read("tu_a", "a.py")], gaps=(0.3,))
    ctx = _ctx(adapter)
    ctx.midturn_inbox = True
    midturn_inbox.clear(ctx.session_id)
    accepted: list[bool] = []

    async def send_while_the_answer_streams():
        await asyncio.sleep(0.1)
        accepted.append(midturn_inbox.push(ctx.session_id, "also check b.py"))

    async def go():
        sender = asyncio.create_task(send_while_the_answer_streams())
        chunks = [c async for c in run_turn(ctx)]
        await sender
        return chunks

    loop = asyncio.new_event_loop()
    try:
        chunks = loop.run_until_complete(go())
    finally:
        midturn_inbox.clear(ctx.session_id)
        loop.close()

    assert accepted == [True]
    assert len(adapter.seen) == 3
    assert "also check b.py" in json.dumps(adapter.seen[2])
    # The message's round began with the answer's end (0.3 s after the answer
    # began), well before the caption landed (1.5 s).
    assert adapter.started[2] - adapter.started[1] < 0.8
    assert _labels(chunks) == [_payload("tu_a")]
    assert chunks.count(DONE) == 1 and chunks[-1] == DONE
    assert midturn_inbox.UNANSWERED not in "".join(chunks)


def test_a_pause_does_not_wait_for_the_captions_still_running(monkeypatch):
    """Round 1's caption hangs; round 2 stops for an approval. The [DONE]
    comes at once (not after LABEL_TIMEOUT_S), so the card can be answered."""
    release = threading.Event()
    monkeypatch.setattr(AL, "aux_one_shot", lambda *a, **k: release.wait(5) and "late")
    _tool_pipeline(monkeypatch, pause_on=(1,))
    try:
        chunks, done_after = _run(_ctx(_Adapter([read("tu_1", "a.py")], [read("tu_2", "b.py")])))
    finally:
        release.set()
    assert _labels(chunks) == [] and chunks[-1] == DONE
    assert done_after < AL.LABEL_TIMEOUT_S / 2


def test_a_stop_during_the_end_of_turn_wait_cancels_the_captions(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(AL, "aux_one_shot", lambda *a, **k: release.wait(5) and "late")
    _tool_pipeline(monkeypatch)
    loop = asyncio.new_event_loop()

    async def go():
        seen: list[str] = []

        async def consume():
            async for chunk in run_turn(_ctx(_Adapter([read("tu_a", "a.py")]))):
                seen.append(chunk)

        consumer = asyncio.create_task(consume())
        for _ in range(200):
            if sum(c.startswith('data: {"text"') for c in seen) == 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # the turn now sits in its end-of-turn wait
        started = [t for t in asyncio.all_tasks() if t.get_name() == "activity-label"]
        assert DONE not in seen and len(started) == 1
        consumer.cancel()  # a Stop: the response task is cancelled
        await asyncio.gather(consumer, return_exceptions=True)
        for _ in range(100):
            if all(t.done() for t in started):
                break
            await asyncio.sleep(0.01)
        return started

    try:
        (task,) = loop.run_until_complete(go())
        assert task.cancelled()
    finally:
        release.set()
        loop.close()


def test_a_session_whose_earlier_call_is_still_in_its_thread_starts_no_new_one(monkeypatch):
    release = threading.Event()
    asked: list[str] = []

    def stuck(*a, **k):
        asked.append(k["session_id"])
        release.wait(5)
        return "Too late"

    monkeypatch.setattr(AL, "aux_one_shot", stuck)
    monkeypatch.setattr(AL, "LABEL_TIMEOUT_S", 0.05)
    _tool_pipeline(monkeypatch)
    try:
        two_rounds = _Adapter([read("tu_1", "a.py")], [read("tu_2", "b.py")], gaps=(0.1,))
        chunks, _ = _run(_ctx(two_rounds))
        # Round 1's call was abandoned by the cap but still holds its thread:
        # round 2 started none, in this turn or in the next one.
        _run(_ctx(_Adapter([read("tu_3", "c.py")])))
        assert asked == ["labels"] and _labels(chunks) == []
    finally:
        release.set()
    AL._in_thread["labels"].result(timeout=5)

    # The thread is free again: the next round gets its caption.
    model = _Recorder("Read c.py")
    monkeypatch.setattr(AL, "aux_one_shot", model)
    monkeypatch.setattr(AL, "LABEL_TIMEOUT_S", 3.0)
    chunks, _ = _run(_ctx(_Adapter([read("tu_4", "c.py")])))
    assert [frame["label"] for frame in _labels(chunks)] == ["Read c.py"]


# Fallbacks: the deterministic label stands


@pytest.mark.parametrize("source", ["agent", "workflow", "cron", "voice", "headless", "wake"])
def test_only_the_chat_turn_asks_for_a_caption(monkeypatch, model, source):
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")]), cost_source=source))
    assert model.calls == [] and _labels(chunks) == [] and chunks[-1] == DONE


def test_the_flag_off_starts_no_caption(monkeypatch, model, config):
    config(feature_flags={"activity_labels": False})
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    assert model.calls == [] and _labels(chunks) == [] and chunks[-1] == DONE


def test_local_mode_starts_no_caption(monkeypatch, model, config):
    from server.infrastructure.feature_flags import get_flag_states

    config(model_mode="local")
    assert "Local mode" in get_flag_states()["activity_labels"]["inactive_reason"]
    assert AL.aux_refusal(AL.TASK, main_model_key="opus5.0") is not None
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    assert model.calls == [] and _labels(chunks) == [] and chunks[-1] == DONE


def _start_one(monkeypatch, chat_model: str):
    """start_label_task for one read on ``chat_model``, awaited; the keys
    that start with "local_" are on-device."""
    from server.local import runtime as local_rt
    from server.tool_executor import ToolState

    monkeypatch.setattr(local_rt, "is_local_model", lambda k: bool(k) and k.startswith("local_"))
    states = [ToolState(tool_use=read("tu_a", "a.py"), status="completed", output="x")]
    ctx = SimpleNamespace(cost_source="chat", model_key=chat_model, session_id="labels")

    async def start():
        task = AL.start_label_task(ctx, states, [])
        if task is not None:
            await task
        return task

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(start())
    finally:
        loop.close()


def test_an_on_device_caption_model_starts_no_caption(monkeypatch, model, config):
    """A generation per round would compete with the turn's own on-device
    stream, so a task that resolves to a local key never runs. The same
    round with the task on a cloud key does get a caption."""
    config(auxiliary_models={"activity_label": "local_gemma"})
    assert _start_one(monkeypatch, "opus5.0") is None and model.calls == []
    config(auxiliary_models={"activity_label": "haiku"})
    assert _start_one(monkeypatch, "opus5.0") is not None and len(model.calls) == 1


@pytest.mark.parametrize("mapped", ["main", "haiku"])
def test_a_chat_on_an_on_device_model_gets_no_cloud_caption(monkeypatch, model, config, mapped):
    """Hybrid mode, a chat on an on-device model: the user chose it for this
    chat, so the head of its tool output, its paths and its commands never go
    to cloud Haiku for a caption, whatever the task is mapped to. The same
    round of a chat on a cloud model does get one."""
    config(model_mode="hybrid", auxiliary_models={"activity_label": mapped})
    assert _start_one(monkeypatch, "local_gemma") is None and model.calls == []
    config(model_mode="hybrid", auxiliary_models={"activity_label": "haiku"})
    assert _start_one(monkeypatch, "opus5.0") is not None and len(model.calls) == 1


def test_a_refused_task_starts_no_caption(monkeypatch, model):
    """The flag on, but the task refused here (a cloud key in Local mode)."""
    monkeypatch.setattr(AL, "aux_refusal", lambda *a, **k: "Activity labels need the cloud")
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    assert model.calls == [] and _labels(chunks) == [] and chunks[-1] == DONE


def test_a_timeout_sends_no_frame_and_the_turn_ends_within_the_cap(monkeypatch):
    release = threading.Event()

    def stuck(*a, **k):
        release.wait(5)
        return "Too late"

    monkeypatch.setattr(AL, "aux_one_shot", stuck)
    monkeypatch.setattr(AL, "LABEL_TIMEOUT_S", 0.05)
    _tool_pipeline(monkeypatch)
    try:
        chunks, done_after = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    finally:
        release.set()
    assert _labels(chunks) == [] and chunks[-1] == DONE
    assert done_after < 1.0


@pytest.mark.parametrize("reply", [RuntimeError("throttled"), "", '  ".  " '])
def test_an_error_or_an_empty_reply_sends_no_frame(monkeypatch, model, reply):
    model.reply = reply
    _tool_pipeline(monkeypatch)
    chunks, _ = _run(_ctx(_Adapter([read("tu_a", "a.py")])))
    assert len(model.calls) == 1
    assert _labels(chunks) == [] and chunks[-1] == DONE


def test_a_stopped_turn_cancels_the_captions_still_running(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(AL, "aux_one_shot", lambda *a, **k: release.wait(5) and "late")
    _tool_pipeline(monkeypatch)
    loop = asyncio.new_event_loop()

    async def go():
        turn = run_turn(_ctx(_Adapter([read("tu_a", "a.py")], gaps=(5,))))
        started: list[asyncio.Task] = []
        async for chunk in turn:
            if chunk.startswith('data: {"text"'):
                started = [t for t in asyncio.all_tasks() if t.get_name() == "activity-label"]
                break
        await turn.aclose()  # a Stop: the stream is closed mid-turn
        for _ in range(100):
            if all(t.done() for t in started):
                break
            await asyncio.sleep(0.01)
        return started

    try:
        (task,) = loop.run_until_complete(go())
        assert task.cancelled()
    finally:
        release.set()
        loop.close()


# Cleaning and billing


@pytest.mark.parametrize(
    ("reply", "label"),
    [
        ('"Fixed the failing auth test."', "Fixed the failing auth test"),
        ("\u201cRead the config\u201d", "Read the config"),
        ("  Ran   the\n test suite. ", "Ran the test suite"),
        ("Edited app.py \u2014 tests green", "Edited app.py, tests green"),
        ("'Checked the build'.", "Checked the build"),
        ("**Fixed** the `auth` test", "Fixed the auth test"),
        ("_Read_ the *config*.", "Read the config"),
        ("Ran npm test \u2705", "Ran npm test"),
        ("\U0001f680 Shipped it \u2714\ufe0f", "Shipped it"),
        ("Moved config \u2192 settings", "Moved config settings"),
        ("Checked the build\u2026", "Checked the build"),
        ("Edited __init__.py and snake_case_name", "Edited __init__.py and snake_case_name"),
        ("Searched *.py files", "Searched *.py files"),
        ("\u2705 \u2728", None),
        ("", None),
        ('"."', None),
        (None, None),
    ],
)
def test_the_reply_is_cleaned_to_one_short_line(reply, label):
    assert AL.clean_label(reply) == label


def test_a_long_reply_is_cut_at_a_word_within_the_limit():
    reply = "Updated " + " ".join(f"module{i}.py" for i in range(20)) + "."
    label = AL.clean_label(reply)
    assert label is not None and len(label) <= AL.MAX_LABEL_CHARS
    assert reply.startswith(label) and label.endswith(".py")


def test_a_claude_caption_runs_on_the_captions_own_client(monkeypatch, config):
    """No retry and a short read timeout, in the region from Settings, and
    never the shared chat client: botocore keeps one adaptive rate limiter
    per client, so throttled captions there would slow the chat."""
    from server.chat import infra
    from server.costs import calls as calls_mod
    from server.infrastructure import aws_clients, oneshot

    config(model_mode="hybrid", bedrock_region="us-west-2")
    built: list[tuple] = []

    def fake_cached_client(cache, lock, key, service, **kw):
        built.append((key, service, kw))
        return "captions client"

    def shared_client():
        raise AssertionError("a caption must not use the shared chat client")

    used: list = []

    def fake_invoke(client, **kw):
        used.append(client)
        return {"content": [{"type": "text", "text": "Read a.py"}]}

    monkeypatch.setattr(aws_clients, "cached_client", fake_cached_client)
    monkeypatch.setattr(infra, "_get_bedrock_client", shared_client)
    monkeypatch.setattr(infra, "_get_chat_models", lambda: {"haiku": "h-id"})
    monkeypatch.setattr(calls_mod, "invoke_claude", fake_invoke)

    reply = oneshot.one_shot(
        "s",
        "u",
        max_tokens=8,
        engine="cloud",
        cloud_model_key="haiku",
        source="activity_label",
        get_bedrock_client=AL._caption_client,
    )
    assert reply == "Read a.py" and used == ["captions client"]
    ((region, service, kw),) = built
    assert (region, service, kw["region_name"]) == ("us-west-2", "bedrock-runtime", "us-west-2")
    assert kw["config"].retries == {"total_max_attempts": 1, "mode": "standard"}
    assert kw["config"].read_timeout == AL.CLIENT_READ_TIMEOUT_S


def test_the_captions_are_billed_under_their_own_source():
    source = TASK_SOURCES[AL.TASK]
    assert require_source(source) == "activity_label"
    assert SOURCE_LABELS[source] == "Activity labels"


def test_start_label_task_never_raises_into_the_turn():
    ctx = SimpleNamespace(cost_source="chat", model_key="m", session_id="s")
    # Not ToolStates: the round is skipped, not crashed.
    assert AL.start_label_task(ctx, [{"id": "x"}], []) is None
