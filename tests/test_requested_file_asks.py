"""The requested-file check reads what the user typed, and only an explicit ask.

Real session: "summarize" over a meeting was answered in chat, then the
completion gate demanded a file because a sentence inside the meeting
transcript read like a request. The model told the user "the quoted phrase is
part of the meeting transcript, not a request to save the summary". Another
time the same nudge was obeyed and the summary landed in Documents as a .txt.

So the gate reads what the user typed (TurnContext.asked, kept across an
approval pause), not the transcript, attachments and inlined mentions the chat
route puts in the same message; a summary, report or document asked for in
words is no file request; and the nudge never picks a folder.
"""

import asyncio

import pytest

import server.chat.engine.runner as runner_mod
from server.chat import routes, stream_slot
from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, _midturn_text, _remind, run_turn
from server.goals import requested_files as rf

_MEETING = (
    "[Transcript so far]\n"
    "Speaker 1: can you export the numbers to a pdf for the board?\n"
    "Speaker 2: sure, and save the deck to the shared drive too."
)


def _prompt_row(typed: str) -> dict:
    """The prompt's message as the chat route builds it: the transcript, then
    what the user typed."""
    return {"role": "user", "content": f"{_MEETING}\n\n{typed}"}


# ── what counts as asking for a file ───────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "summarize this for me, and make sure you write what was said at the start",
        "can you write me briefly all the questions about data quality, keep it short",
        "write me a summary report of the meeting",
        "create a document with the action items",
        "give me a new version of the plan",
        "make a copy of the notes with the dates fixed",
        "write it in markdown",
        "give me the json for the config",
        # Where a file already is, or what is in a folder, asks for nothing.
        "I saved the report in Downloads, can you read it",
        "give me a list of what is in my downloads folder",
    ],
)
def test_asking_for_words_is_not_asking_for_a_file(prompt):
    assert rf.requested_clauses(prompt) == []


@pytest.mark.parametrize(
    ("prompt", "disk"),
    [
        ("save the summary as a file", True),
        ("make me a pdf of the notes", True),
        ("write the notes to ~/Desktop/notes.md", True),
        ("export the totals", True),
        # Saving somewhere asks for a file without naming one (a real ask).
        ("produce a version that can be saved locally and shared", True),
        ("save it to my desktop", True),
        ("draw a diagram of the flow", False),
        ("make a slide deck from this", False),
    ],
)
def test_a_file_is_owed_on_disk_only_when_the_user_names_one(prompt, disk):
    (clause,) = rf.requested_clauses(prompt)
    assert rf.wants_disk(clause) is disk


def test_a_named_file_type_is_not_answered_by_a_card_in_the_chat():
    msgs = [
        {"role": "user", "content": "make me a pdf of the notes"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "create_artifact", "input": {}}],
        },
        {"role": "assistant", "content": "Here it is."},
    ]
    assert rf.requested_file_feedback(msgs, None) is not None


# ── the transcript is not the user's ask ───────────────────────────────────


def test_the_transcript_beside_the_question_asks_for_nothing():
    msgs = [_prompt_row("summarize"), {"role": "assistant", "content": "The board wants numbers."}]
    # Read whole, the meeting's own words look like requests.
    assert rf.requested_file_feedback(msgs, None) is not None
    # What the user typed asks for a summary, which the reply is.
    assert rf.file_requests(msgs, "summarize") == []
    assert rf.requested_file_feedback(msgs, None, asked="summarize") is None


def test_the_typed_ask_still_carries_a_message_sent_mid_turn():
    msgs = [_prompt_row("summarize")]
    _remind(msgs, _midturn_text(midturn_inbox.Entry(midturn_inbox.USER, "also save it as a pdf")))
    assert rf.file_requests(msgs, "summarize") == [("also save it as a pdf", 0)]


def test_an_ask_the_user_typed_is_still_checked():
    msgs = [_prompt_row("save the summary as a file"), {"role": "assistant", "content": "Done."}]
    fb = rf.requested_file_feedback(msgs, None, asked="save the summary as a file")
    assert fb and "save the summary as a file" in fb and "Speaker" not in fb


@pytest.mark.parametrize(
    ("typed", "expect"),
    [
        ("give me a diagram of the pipeline", ("create_visual", "do not save it as a file")),
        ("can you make me a pdf of this?", ("save_file",)),
    ],
)
def test_the_nudge_never_picks_a_folder_for_the_user(typed, expect):
    msgs = [{"role": "user", "content": typed}, {"role": "assistant", "content": "In words: ..."}]
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and "Downloads" not in fb
    assert all(part in fb for part in expect)


# ── the gate and the engine read the typed words ───────────────────────────


@pytest.fixture
def _gate(monkeypatch):
    from server import hooks
    from server.goals import gate

    async def no_stop(*a, **k):
        from types import SimpleNamespace

        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "requested_file_check")
    return gate


def test_the_gate_reads_what_the_user_typed(_gate):
    from server.goals import GateContext

    msgs = [_prompt_row("summarize"), {"role": "assistant", "content": "The board wants numbers."}]
    whole = asyncio.run(_gate.run_completion_gate(GateContext(session_id="s", messages=msgs)))
    typed = asyncio.run(
        _gate.run_completion_gate(GateContext(session_id="s", messages=msgs, asked="summarize"))
    )
    assert whole.block is True and typed.block is False


class _Answers:
    """Answers in text once; asks for an approval first when told to."""

    provider = "test"

    def __init__(self, pause: bool = False):
        self.pause = pause
        self.calls = 0

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls += 1
        if self.pause:
            content = [{"type": "tool_use", "id": "t1", "name": "ws_write_file", "input": {}}]
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        yield TextDelta(text="The board wants numbers.")
        content = [{"type": "text", "text": "The board wants numbers."}]
        yield RoundResult(stop_reason="end_turn", content=content, usage=Usage())


def _ctx(adapter, asked, sid="answer-in-chat"):
    return TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[_prompt_row("summarize")],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=5, completion_gate=True),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        asked=asked,
    )


def _run(ctx) -> str:
    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def test_the_engine_hands_the_typed_words_to_the_gate(_gate):
    typed = _Answers()
    out = _run(_ctx(typed, "summarize"))
    assert "stop_hook_block" not in out and typed.calls == 1
    # Without them the meeting's words are read as the ask: one nudge.
    whole = _Answers()
    out = _run(_ctx(whole, None))
    assert out.count("stop_hook_block") == 1 and whole.calls == 2


def test_a_paused_turn_keeps_the_typed_words(monkeypatch):
    import server.tool_executor as TE
    from server.chat.engine.pause import paused_sessions

    async def batch(tool_uses, **kw):
        return list(tool_uses)

    async def process(states, budget_fn, **kw):
        results = [{"type": "tool_result", "tool_use_id": t["id"], "content": ""} for t in states]
        return (results, [], False, True)

    monkeypatch.setattr(TE, "execute_tool_batch", batch)
    monkeypatch.setattr(TE, "process_tool_results", process)
    paused_sessions.pop("paused-ask", None)
    try:
        _run(_ctx(_Answers(pause=True), "summarize", sid="paused-ask"))
        assert paused_sessions["paused-ask"]["asked"] == "summarize"
    finally:
        paused_sessions.pop("paused-ask", None)


# ── the chat route ─────────────────────────────────────────────────────────


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def is_disconnected(self):
        return False


def _route_turn(monkeypatch, body) -> TurnContext:
    seen: list = []

    async def spy(ctx):
        seen.append(ctx)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runner_mod, "run_turn", spy)

    async def go():
        resp = await routes.chat_endpoint(_Request(body))
        async for _chunk in resp.body_iterator:
            pass

    try:
        asyncio.run(go())
    finally:
        stream_slot.active_streams.clear()
        stream_slot.heartbeats.clear()
    assert seen, "the route never started the turn"
    return seen[0]


def test_the_chat_route_passes_what_the_user_typed(monkeypatch):
    body = {
        "question": "summarize",
        "transcript": "Speaker 1: can you export the numbers to a pdf for the board?",
        "session_id": "route-asked",
        "history": [],
    }
    ctx = _route_turn(monkeypatch, body)
    assert ctx.asked == "summarize"
    # The model still reads the transcript; only the gate's reading changed.
    assert "export the numbers to a pdf" in str(ctx.messages[-1]["content"])


def test_the_chat_route_hands_the_typed_words_to_the_on_device_route(monkeypatch):
    import server.local.route as local_route

    handed: list = []

    def not_local(**kw):
        handed.append(kw.get("asked"))
        return None  # not an on-device model: the cloud turn runs instead

    monkeypatch.setattr(local_route, "local_chat_response", not_local)
    body = {"question": "summarize", "session_id": "route-local", "history": []}
    _route_turn(monkeypatch, body)
    assert handed == ["summarize"]


def test_the_on_device_route_passes_the_typed_words_on(monkeypatch):
    from server.local import route as local_route
    from server.local import serving

    seen: list = []

    async def serve_turn(model_key, n_ctx=None, **kw):
        return "http://127.0.0.1:9"

    async def spy(ctx):
        seen.append(ctx)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k == "local_gemma")
    monkeypatch.setattr(serving, "serve_turn", serve_turn)
    monkeypatch.setattr(serving, "wire_model", lambda k: k)
    monkeypatch.setattr(serving, "supports_tool_choice", lambda k: False)
    monkeypatch.setattr(runner_mod, "run_turn", spy)
    resp = local_route.local_chat_response(
        model_key="local_gemma",
        body={},
        messages=[_prompt_row("summarize")],
        session_id="local-asked",
        approved_tool_result=None,
        transcript="",
        asked="summarize",
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

    async def drain():
        return [c async for c in resp.body_iterator]

    asyncio.run(drain())
    assert seen and seen[0].asked == "summarize"


def test_a_continuation_takes_the_typed_words_back_from_the_pause(monkeypatch):
    routes._paused_sessions["route-resume"] = {
        "messages": [
            {"role": "user", "content": "save the summary as a file"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "ws_write_file", "input": {}}],
            },
        ],
        "pending_tool_results": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}],
        "provider": "anthropic",
        "asked": "save the summary as a file",
    }
    body = {
        "question": "",
        "session_id": "route-resume",
        "history": [],
        "approved_tool_result": {"tool_use_id": "t1", "content": "ok"},
    }
    try:
        ctx = _route_turn(monkeypatch, body)
    finally:
        routes._paused_sessions.pop("route-resume", None)
    assert ctx.asked == "save the summary as a file"
