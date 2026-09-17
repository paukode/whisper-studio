"""VoiceSession against a scripted fake Sonic stream: preamble order, transcript
bookkeeping, tool round trip, interruption, stream renewal with history
replay. No network, no SDK."""

import asyncio
import base64
import functools
import json

import pytest

from server.voice import protocol
from server.voice import session as voice_session
from server.voice.session import VoiceConfig, VoiceSession


class FakeStream:
    """Records every event the session sends; ``feed`` queues model output."""

    def __init__(self):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, event_json: str) -> None:
        self.sent.append(json.loads(event_json)["event"])

    async def receive(self):
        item = await self.incoming.get()
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)

    # -- helpers for tests --
    def feed(self, body: dict) -> None:
        self.incoming.put_nowait(json.dumps({"event": body}).encode())

    def feed_text_block(self, cid, role, text, stage=None, stop="END_TURN"):
        cs = {"contentId": cid, "role": role, "type": "TEXT"}
        if stage:
            cs["additionalModelFields"] = json.dumps({"generationStage": stage})
        self.feed({"contentStart": cs})
        self.feed({"textOutput": {"contentId": cid, "content": text}})
        self.feed({"contentEnd": {"contentId": cid, "stopReason": stop}})

    def kinds(self):
        return [next(iter(e)) for e in self.sent]


@pytest.fixture
def harness(monkeypatch):
    # An utterance commits once its speech has ended plus a quiet margin (or
    # at a turn boundary); keep the margins tiny so the tests stay quick.
    monkeypatch.setattr(voice_session, "UTTERANCE_QUIET_S", 0.05)
    monkeypatch.setattr(voice_session, "UTTERANCE_MAX_WAIT_S", 0.3)
    monkeypatch.setattr(voice_session, "INTERRUPT_GRACE_S", 0.05)
    # A hang-up keeps a run waiting on the user's card alive this long.
    monkeypatch.setattr(voice_session, "PENDING_DRAIN_S", 0.2)
    streams: list[FakeStream] = []
    events: list[dict] = []
    clock = {"t": 1000.0}

    async def opener(model_id, region):
        s = FakeStream()
        streams.append(s)
        return s

    async def emit(ev):
        events.append(ev)

    def make(history=None, **kw):
        return VoiceSession(
            session_id="s1",
            config=VoiceConfig(voice_id="matthew", system_prompt="SYS"),
            emit=emit,
            history=history,
            opener=opener,
            clock=lambda: clock["t"],
            **kw,
        )

    return {"streams": streams, "events": events, "make": make, "clock": clock}


def run_async(fn):
    """pytest-asyncio is not a dependency here; drive each case with asyncio.run."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _texts(events):
    """assistant_text events without their utterance ids (ids are random)."""
    return [
        {k: v for k, v in e.items() if k != "utterance_id"}
        for e in events
        if e["type"] == "assistant_text"
    ]


async def _settle(n=5):
    for _ in range(n):
        await asyncio.sleep(0)
    # Let the debounced final-text flush fire.
    await asyncio.sleep(0.12)


@run_async
async def test_start_sends_preamble_in_order_with_history(harness):
    vs = harness["make"](
        history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    )
    await vs.start()
    s = harness["streams"][0]
    kinds = s.kinds()
    assert kinds[:2] == ["sessionStart", "promptStart"]
    # system prompt, 2 history rows, then the live audio block opens last
    assert kinds[2:5] == ["contentStart", "textInput", "contentEnd"]
    assert s.sent[2]["contentStart"]["role"] == "SYSTEM"
    assert s.sent[3]["textInput"]["content"] == "SYS"
    assert s.sent[5]["contentStart"]["role"] == "USER" and s.sent[6]["textInput"]["content"] == "hi"
    assert s.sent[8]["contentStart"]["role"] == "ASSISTANT"
    assert s.sent[-1]["contentStart"]["type"] == "AUDIO"
    assert s.sent[1]["promptStart"]["audioOutputConfiguration"]["voiceId"] == "matthew"
    tool_names = [
        t["toolSpec"]["name"] for t in s.sent[1]["promptStart"]["toolConfiguration"]["tools"]
    ]
    assert tool_names == [
        "ask_assistant",
        "resolve_request",
        "background_status",
        "control_recording",
        "end_conversation",
    ]
    assert harness["events"][0]["type"] == "ready"
    await vs.stop()
    assert harness["events"][-1] == {"type": "ended", "reason": "user"}
    assert s.kinds()[-3:] == ["contentEnd", "promptEnd", "sessionEnd"]
    assert s.closed


@run_async
async def test_audio_and_typed_text_reach_the_stream(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    await vs.send_audio(b"\x00\x01" * 8)
    audio = s.sent[-1]["audioInput"]
    assert base64.b64decode(audio["content"]) == b"\x00\x01" * 8
    assert audio["contentName"] == s.sent[-2]["contentStart"]["contentName"]
    await vs.send_text("  run the tests ")
    assert s.sent[-3]["contentStart"]["interactive"] is True
    assert s.sent[-2]["textInput"]["content"] == "run the tests"
    assert vs.transcript[-1] == {"role": "user", "content": "run the tests"}
    typed = [e for e in harness["events"] if e["type"] == "user_transcript"]
    assert typed == [{"type": "user_transcript", "text": "run the tests", "typed": True}]
    await vs.stop()


@run_async
async def test_output_events_become_transcript_and_ui_events(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"completionStart": {}})
    s.feed_text_block("u1", "USER", "Run the test suite.", "FINAL")
    s.feed_text_block("a1", "ASSISTANT", "Sure, on it.", "SPECULATIVE")
    s.feed({"contentStart": {"contentId": "au", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au", "content": base64.b64encode(b"pcm!").decode()}})
    s.feed({"contentEnd": {"contentId": "au", "stopReason": "END_TURN"}})
    s.feed_text_block("a2", "ASSISTANT", "Sure, on it.", "FINAL")
    s.feed({"completionEnd": {"stopReason": "END_TURN"}})
    await _settle(30)
    ev = harness["events"]
    types = [e["type"] for e in ev]
    assert {"type": "user_transcript", "text": "Run the test suite.", "typed": False} in ev
    assert {"type": "assistant_text", "text": "Sure, on it.", "final": False} in _texts(ev)
    assert {"type": "audio", "pcm": b"pcm!"} in ev
    assert {"type": "assistant_text", "text": "Sure, on it.", "final": True} in _texts(ev)
    # speaking while audio flowed, listening again after the turn (the final
    # text is committed on the turn boundary, so it may follow the state event)
    assert types.index("audio") > types.index("state")
    states = [e["state"] for e in ev if e["type"] == "state"]
    assert "speaking" in states and states[-1] == "listening"
    assert vs.transcript == [
        {"role": "user", "content": "Run the test suite."},
        {"role": "assistant", "content": "Sure, on it."},
    ]
    await vs.stop()


@run_async
async def test_turn_end_without_final_block_commits_speculative_text(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"completionStart": {}})
    s.feed_text_block("a1", "ASSISTANT", "Hello ", "SPECULATIVE")
    s.feed_text_block("a2", "ASSISTANT", "there.", "SPECULATIVE")
    s.feed({"completionEnd": {"stopReason": "END_TURN"}})
    await _settle(30)
    finals = [e for e in _texts(harness["events"]) if e["final"]]
    assert finals == [{"type": "assistant_text", "text": "Hello there.", "final": True}]
    await vs.stop()


@run_async
async def test_interrupt_is_forwarded(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"contentStart": {"contentId": "au", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au", "content": base64.b64encode(b"x").decode()}})
    s.feed({"contentEnd": {"contentId": "au", "stopReason": "INTERRUPTED"}})
    await _settle(20)
    types = [e["type"] for e in harness["events"]]
    assert "interrupted" in types
    assert harness["events"][-1] == {"type": "state", "state": "listening"}
    await vs.stop()


@run_async
async def test_tool_use_runs_tool_and_answers_on_the_same_stream(harness, monkeypatch):
    calls = []

    async def fake_run_tool(name, tool_input, ctx):
        calls.append((name, tool_input))
        await ctx.emit({"type": "assistant_step", "name": "ws_glob", "status": "ok", "detail": "x"})
        return "214 tests, 2 failed"

    monkeypatch.setattr(voice_session, "run_tool", fake_run_tool)
    vs = harness["make"](model_key="opus5.0")
    await vs.start()
    s = harness["streams"][0]
    s.feed(
        {
            "toolUse": {
                "toolUseId": "tu-9",
                "toolName": "ask_assistant",
                "content": json.dumps({"request": "run the tests"}),
            }
        }
    )
    await _settle(40)
    assert calls == [("ask_assistant", {"request": "run the tests"})]
    ev = harness["events"]
    assert {
        "type": "tool_call",
        "tool_use_id": "tu-9",
        "name": "ask_assistant",
        "input": {"request": "run the tests"},
    } in ev
    assert {"type": "assistant_step", "name": "ws_glob", "status": "ok", "detail": "x"} in ev
    assert {
        "type": "tool_result",
        "tool_use_id": "tu-9",
        "name": "ask_assistant",
        "output": "214 tests, 2 failed",
        "status": "ok",
    } in ev
    # thinking while the tool ran, listening after
    states = [e["state"] for e in ev if e["type"] == "state"]
    assert states[-2:] == ["thinking", "listening"] or states[-1] == "listening"
    tool_events = [e for e in s.sent if "toolResult" in e]
    # Sonic only accepts a JSON object as tool result content.
    assert json.loads(tool_events[0]["toolResult"]["content"]) == {"result": "214 tests, 2 failed"}
    start = [e for e in s.sent if "contentStart" in e and e["contentStart"].get("type") == "TOOL"]
    assert start[0]["contentStart"]["toolResultInputConfiguration"]["toolUseId"] == "tu-9"
    await vs.stop()


@run_async
async def test_renewal_reopens_with_transcript_and_reroutes_late_tool_result(harness, monkeypatch):
    gate = asyncio.Event()

    async def slow_tool(name, tool_input, ctx):
        await gate.wait()
        return "late answer"

    monkeypatch.setattr(voice_session, "run_tool", slow_tool)
    monkeypatch.setattr(voice_session, "RENEW_AFTER_S", 10)
    monkeypatch.setattr(voice_session, "RENEW_DEADLINE_S", 20)
    vs = harness["make"]()
    await vs.start()
    s1 = harness["streams"][0]
    s1.feed_text_block("u1", "USER", "hello", "FINAL")
    s1.feed({"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}})
    await _settle(20)
    # Past the deadline with a tool still pending: renewal must happen anyway.
    harness["clock"]["t"] += 25
    for _ in range(40):
        await asyncio.sleep(0.05)
        if len(harness["streams"]) == 2:
            break
    assert len(harness["streams"]) == 2
    s2 = harness["streams"][1]
    assert s1.closed
    # The new stream was seeded with the transcript so far.
    seeded = [e["textInput"]["content"] for e in s2.sent if "textInput" in e]
    assert seeded[0] == "SYS" and "hello" in seeded
    types = [e["type"] for e in harness["events"]]
    assert "renewing" in types and "renewed" in types
    # The late tool result cannot answer a toolUse the new stream never saw:
    # it arrives as a typed turn instead, and the old stream gets nothing.
    gate.set()
    await _settle(40)
    late = [e for e in s2.sent if "textInput" in e and "late answer" in e["textInput"]["content"]]
    assert (
        late and "[Result of your earlier ask_assistant request]" in late[0]["textInput"]["content"]
    )
    assert not any("toolResult" in e for e in s1.sent)
    await vs.stop()
    assert s2.closed


@run_async
async def test_model_closing_the_stream_triggers_reopen(harness):
    vs = harness["make"]()
    await vs.start()
    s1 = harness["streams"][0]
    s1.feed_text_block("u1", "USER", "hello", "FINAL")  # a healthy stream first
    await _settle(10)
    await s1.incoming.put(None)  # model hung up
    for _ in range(40):
        await asyncio.sleep(0.02)
        if len(harness["streams"]) == 2:
            break
    assert len(harness["streams"]) == 2
    await vs.stop()


@run_async
async def test_stream_error_reopens_instead_of_ending(harness):
    """A failed stream is renewed with the transcript; the conversation (and any
    background run) survives. Observed live: Sonic's idle cut after 295 s."""
    from server.voice.sonic_client import SonicStreamError

    vs = harness["make"]()
    await vs.start()
    s1 = harness["streams"][0]
    s1.feed_text_block("u1", "USER", "hello", "FINAL")
    await _settle(10)

    async def boom():
        raise SonicStreamError("ValidationException: Timed out waiting for audio bytes")

    s1.receive = boom  # type: ignore[assignment]
    s1.feed({"completionStart": {"promptName": "p"}})  # wake the pump; next receive raises
    for _ in range(40):
        await asyncio.sleep(0.02)
        if len(harness["streams"]) == 2:
            break
    assert len(harness["streams"]) == 2 and s1.closed
    assert not vs.done.is_set()
    s2 = harness["streams"][1]
    seeded = [e["textInput"]["content"] for e in s2.sent if "textInput" in e]
    assert "hello" in seeded
    types = [e["type"] for e in harness["events"]]
    assert "renewing" in types and "renewed" in types and "error" not in types
    await vs.stop()


@run_async
async def test_first_stream_failing_before_any_event_is_a_start_failure(harness):
    """Bedrock refusing the very first request (model not enabled, wrong
    region, blocked transport) is reported at once with the real error, not
    retried six times and then blamed on a renewal."""
    from server.voice.sonic_client import SonicStreamError

    async def boom():
        raise SonicStreamError("ValidationException: model access denied")

    vs = harness["make"]()
    real_opener = vs._open_stream

    async def failing_opener(model_id, region):
        s = await real_opener(model_id, region)
        s.receive = boom  # type: ignore[assignment]
        return s

    vs._open_stream = failing_opener
    await vs.start()
    for _ in range(200):
        await asyncio.sleep(0.02)
        if vs.done.is_set():
            break
    assert vs.done.is_set()
    assert len(harness["streams"]) == 1
    errors = [e for e in harness["events"] if e["type"] == "error"]
    assert errors and errors[0]["message"].startswith("Voice could not start")
    assert "model access denied" in errors[0]["message"]
    assert "renewed" not in errors[0]["message"]
    assert harness["events"][-1] == {"type": "ended", "reason": "error"}


@run_async
async def test_persistent_stream_failures_after_a_healthy_start_end_the_session(harness):
    from server.voice.sonic_client import SonicStreamError

    async def boom():
        raise SonicStreamError("ValidationException: model access denied")

    vs = harness["make"]()
    real_opener = vs._open_stream

    async def failing_opener(model_id, region):
        s = await real_opener(model_id, region)
        s.receive = boom  # type: ignore[assignment]
        return s

    await vs.start()
    s1 = harness["streams"][0]
    s1.feed_text_block("u1", "USER", "hello", "FINAL")
    await _settle(10)
    # From here every stream, the live one included, fails right away.
    vs._open_stream = failing_opener
    s1.receive = boom  # type: ignore[assignment]
    s1.feed({"completionStart": {"promptName": "p"}})
    for _ in range(200):
        await asyncio.sleep(0.02)
        if vs.done.is_set():
            break
    assert vs.done.is_set()
    assert len(harness["streams"]) == voice_session.MAX_REOPENS + 1
    errors = [e for e in harness["events"] if e["type"] == "error"]
    assert errors and "could not be renewed" in errors[0]["message"]
    assert "model access denied" in errors[0]["message"]
    assert harness["events"][-1] == {"type": "ended", "reason": "error"}


@run_async
async def test_idle_silent_input_renews_before_sonic_cuts_the_stream(harness):
    """All-zero audio does not count as input for Sonic; audio with signal or
    text does. Only a truly idle stream is renewed."""
    monkeypatch_idle = 10
    voice_session.IDLE_RENEW_S, saved = monkeypatch_idle, voice_session.IDLE_RENEW_S
    try:
        vs = harness["make"]()
        await vs.start()
        s1 = harness["streams"][0]
        # Signal keeps the stream: advance past the idle window with live audio.
        harness["clock"]["t"] += 8
        await vs.send_audio(b"\x00\x10" * 320)  # a real signal (peak 4096) resets idle
        harness["clock"]["t"] += 8
        await vs.send_audio(b"\x05\x00" * 320)  # a quiet room's noise floor does not count
        await vs.send_audio(bytes(640))  # nor does silence
        await asyncio.sleep(1.2)
        assert len(harness["streams"]) == 1  # idle is 8 s: under the window
        # Now nothing but silence for the whole window.
        harness["clock"]["t"] += 10
        await vs.send_audio(bytes(640))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if len(harness["streams"]) == 2:
                break
        assert len(harness["streams"]) == 2 and s1.closed
        await vs.stop()
    finally:
        voice_session.IDLE_RENEW_S = saved


def test_system_prompt_mentions_workspace_and_tools():
    text = voice_session.build_system_prompt(workspace_name="ledger-toolkit")
    assert "ledger-toolkit" in text
    for tool in ("ask_assistant", "control_recording", "end_conversation"):
        assert tool in text


@run_async
async def test_final_text_block_ends_speaking_without_completion_end(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed_text_block("a1", "ASSISTANT", "Hello.", "SPECULATIVE")
    s.feed({"contentStart": {"contentId": "au", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au", "content": base64.b64encode(b"x").decode()}})
    s.feed({"contentEnd": {"contentId": "au", "stopReason": "END_TURN"}})
    s.feed_text_block("a2", "ASSISTANT", "Hello.", "FINAL")
    await _settle(30)
    ev = harness["events"]
    states = [e["state"] for e in ev if e["type"] == "state"]
    assert states[-1] == "listening"
    finals = [e for e in _texts(ev) if e["final"]]
    assert finals == [{"type": "assistant_text", "text": "Hello.", "final": True}]
    # A later speculative-only turn is flushed when the session stops.
    s.feed_text_block("a3", "ASSISTANT", "Bye now.", "SPECULATIVE")
    await _settle(20)
    await vs.stop()
    finals = [e for e in harness["events"] if e["type"] == "assistant_text" and e["final"]]
    assert finals[-1]["text"] == "Bye now."
    # Consecutive assistant pieces are one turn in the renewal transcript.
    assert vs.transcript[-1] == {"role": "assistant", "content": "Hello. Bye now."}


@run_async
async def test_consecutive_speech_pieces_merge_into_one_turn(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed_text_block("u1", "USER", "just run", "FINAL")
    s.feed_text_block("u2", "USER", "git branch", "FINAL")
    s.feed_text_block("u3", "USER", "and show me the output", "FINAL")
    s.feed_text_block("a1", "ASSISTANT", "On it.", "FINAL")
    s.feed_text_block("a2", "ASSISTANT", "Here you go.", "FINAL")
    await _settle(40)
    assert vs.transcript == [
        {"role": "user", "content": "just run git branch and show me the output"},
        {"role": "assistant", "content": "On it. Here you go."},
    ]
    # The browser still receives every piece (it merges them the same way).
    pieces = [e["text"] for e in harness["events"] if e["type"] == "user_transcript"]
    assert pieces == ["just run", "git branch", "and show me the output"]
    await vs.stop()


@run_async
async def test_ui_resolve_runs_the_tool_and_tells_sonic(harness, monkeypatch):
    seen = []

    async def fake_run_tool(name, tool_input, ctx):
        seen.append((name, tool_input, dict(ctx.pending)))
        ctx.pending.clear()
        return "The assistant continued: folder opened."

    monkeypatch.setattr(voice_session, "run_tool", fake_run_tool)
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    vs.pending.update({"type": "approval_request", "tool_use_id": "t1", "run_id": "r1"})
    vs.resolve_pending_from_ui("approve")
    await _settle(30)
    assert seen and seen[0][0] == "resolve_request" and seen[0][1] == {"decision": "approve"}
    results = [e for e in harness["events"] if e["type"] == "tool_result"]
    assert results and results[-1]["name"] == "resolve_request" and results[-1]["status"] == "ok"
    typed = [e["textInput"]["content"] for e in s.sent if "textInput" in e]
    assert any("already answered the pending request on screen: approve" in t for t in typed)
    assert any("Outcome of the request the user answered on screen" in t for t in typed)
    await vs.stop()


@run_async
async def test_interrupt_marker_text_is_not_speech(harness):
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed_text_block("a1", "ASSISTANT", "The branches are main and", "SPECULATIVE")
    s.feed({"contentStart": {"contentId": "au", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"contentEnd": {"contentId": "au", "stopReason": "INTERRUPTED"}})
    s.feed_text_block("a2", "ASSISTANT", '{ "interrupted" : true }', "FINAL")
    s.feed_text_block("a3", "ASSISTANT", "The branches are main and", "FINAL")
    await _settle(40)
    finals = [e["text"] for e in harness["events"] if e["type"] == "assistant_text" and e["final"]]
    assert finals == ["The branches are main and"]
    assert vs.transcript == [{"role": "assistant", "content": "The branches are main and"}]
    await vs.stop()


@run_async
async def test_paused_delegate_result_is_marked_paused(harness, monkeypatch):
    async def pausing_tool(name, tool_input, ctx):
        ctx.pending.update({"type": "approval_request", "tool_use_id": "t1", "run_id": "r1"})
        return "The assistant paused: it needs the user's approval to run pytest."

    monkeypatch.setattr(voice_session, "run_tool", pausing_tool)
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}})
    await _settle(40)
    results = [e for e in harness["events"] if e["type"] == "tool_result"]
    assert results and results[0]["status"] == "paused"
    # Sonic still gets the guidance text as the tool result.
    sent = [e for e in s.sent if "toolResult" in e]
    assert "needs the user's approval" in sent[0]["toolResult"]["content"]
    await vs.stop()


@run_async
async def test_background_run_is_marked_working_and_its_late_result_is_typed_to_sonic(
    harness, monkeypatch
):
    """A long delegated run answers "Working on it" (status working, no bubble);
    when it finishes, the tool's deliver_late hook types the result to Sonic as
    an interactive USER turn so it can speak the outcome."""
    captured = {}

    async def slow_tool(name, tool_input, ctx):
        captured["ctx"] = ctx
        return "Working on it: check for bugs. It is running in the background."

    monkeypatch.setattr(voice_session, "run_tool", slow_tool)
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}})
    await _settle(40)
    results = [e for e in harness["events"] if e["type"] == "tool_result"]
    assert results and results[0]["status"] == "working"
    ctx = captured["ctx"]
    assert ctx.runs is vs.runs and ctx.pending_queue is vs.pending_queue
    before = len(s.sent)
    await ctx.deliver_late("[Result of the request 'check for bugs':] Found three bugs.")
    typed = [e for e in s.sent[before:] if "textInput" in e]
    assert typed and "Found three bugs." in typed[0]["textInput"]["content"]
    await vs.stop()


@run_async
async def test_sentence_level_final_blocks_commit_as_one_message(harness):
    """Observed live: Sonic sends the whole utterance as SPECULATIVE text, then
    all the audio within seconds, then confirms it as FINAL chunks at speaking
    pace, several seconds apart. The browser must get ONE final per utterance
    (committing per chunk made the live text vanish and stream back in), and
    "speaking" must hold until the speech has ended."""
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed_text_block(
        "a1",
        "ASSISTANT",
        "Sorry, I am still working on it. The list will appear shortly.",
        "SPECULATIVE",
    )
    # 0.4 s of speech worth of audio arrives at once.
    s.feed({"contentStart": {"contentId": "au", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au", "content": base64.b64encode(bytes(19200)).decode()}})
    s.feed({"contentEnd": {"contentId": "au", "stopReason": "END_TURN"}})
    await _settle(10)
    s.feed_text_block("f1", "ASSISTANT", "Sorry, I am still working on it.", "FINAL")
    await asyncio.sleep(0.15)  # a real sentence gap, far longer than the quiet margin
    # Nothing committed yet, still speaking: the speech is not over.
    assert [e for e in _texts(harness["events"]) if e["final"]] == []
    assert [e["state"] for e in harness["events"] if e["type"] == "state"][-1] == "speaking"
    s.feed_text_block("f2", "ASSISTANT", "The list will appear shortly.", "FINAL")
    await asyncio.sleep(0.6)  # past the speech end plus the quiet margin
    finals = [e for e in _texts(harness["events"]) if e["final"]]
    assert [e["text"] for e in finals] == [
        "Sorry, I am still working on it. The list will appear shortly."
    ]
    utt_ids = {e["utterance_id"] for e in harness["events"] if e["type"] == "assistant_text"}
    assert len(utt_ids) == 1
    assert [e["state"] for e in harness["events"] if e["type"] == "state"][-1] == "listening"
    assert vs.transcript == [
        {
            "role": "assistant",
            "content": "Sorry, I am still working on it. The list will appear shortly.",
        }
    ]
    # A late confirmation of what was just committed is not new speech.
    s.feed_text_block("f2b", "ASSISTANT", "The list will appear shortly.", "FINAL")
    await asyncio.sleep(0.2)
    assert len([e for e in _texts(harness["events"]) if e["final"]]) == 1
    # The next utterance gets its own single commit and its own id.
    s.feed_text_block("a2", "ASSISTANT", "Here it is.", "SPECULATIVE")
    s.feed_text_block("f3", "ASSISTANT", "Here it is.", "FINAL")
    await asyncio.sleep(0.2)
    finals = [e for e in _texts(harness["events"]) if e["final"]]
    assert [e["text"] for e in finals] == [
        "Sorry, I am still working on it. The list will appear shortly.",
        "Here it is.",
    ]
    await vs.stop()


@run_async
async def test_confirmed_text_that_is_incomplete_falls_back_to_the_preview(harness):
    """If the quiet deadline fires before the last FINAL chunk, the user heard
    the whole preview: commit that rather than a truncated confirmation."""
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed_text_block("a1", "ASSISTANT", "One two three four five six seven eight.", "SPECULATIVE")
    s.feed_text_block("f1", "ASSISTANT", "One two three", "FINAL")
    s.feed({"completionEnd": {"stopReason": "END_TURN"}})
    await _settle(10)
    finals = [e["text"] for e in _texts(harness["events"]) if e["final"]]
    assert finals == ["One two three four five six seven eight."]
    await vs.stop()


@run_async
async def test_hang_up_keeps_delegated_runs_and_drains_their_answers(harness, monkeypatch):
    """End closes Sonic but never discards work in flight: the browser gets a
    draining notice, the run's events keep flowing, then ended."""
    gate = asyncio.Event()

    async def slow_tool(name, tool_input, ctx):
        async def run():
            await gate.wait()
            await ctx.emit(
                {
                    "type": "assistant_answer",
                    "run_id": "r1",
                    "request": "bugs",
                    "output": "Done.",
                    "status": "ok",
                }
            )
            ctx.runs.pop("r1", None)

        task = asyncio.create_task(run())
        ctx.runs["r1"] = {"request": "check for bugs", "task": task, "detached": True}
        ctx.register_task(task)
        return "Working on it: check for bugs."

    monkeypatch.setattr(voice_session, "run_tool", slow_tool)
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}})
    await _settle(20)
    stop_task = asyncio.create_task(vs.stop("user"))
    await _settle(20)
    types = [e["type"] for e in harness["events"]]
    assert "draining" in types and "ended" not in types
    draining = next(e for e in harness["events"] if e["type"] == "draining")
    assert draining["runs"] == [{"run_id": "r1", "request": "check for bugs"}]
    assert s.closed and not vs.done.is_set()
    gate.set()
    await stop_task
    types = [e["type"] for e in harness["events"]]
    assert types.index("assistant_answer") < types.index("ended")
    assert vs.done.is_set()


@run_async
async def test_socket_gone_cancels_delegated_runs(harness, monkeypatch):
    cancelled = asyncio.Event()

    async def slow_tool(name, tool_input, ctx):
        async def run():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(run())
        ctx.runs["r1"] = {"request": "x", "task": task, "detached": True}
        ctx.register_task(task)
        return "Working on it: x."

    monkeypatch.setattr(voice_session, "run_tool", slow_tool)
    vs = harness["make"]()
    await vs.start()
    harness["streams"][0].feed(
        {"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}}
    )
    await _settle(20)
    await vs.stop("user", cancel_runs=True)
    assert cancelled.is_set() and vs.done.is_set()


@run_async
async def test_stop_forgets_paused_turns_and_malformed_events_are_skipped(harness):
    from server.chat.engine.pause import paused_sessions

    vs = harness["make"]()
    await vs.start()
    s1 = harness["streams"][0]
    # A malformed model event is skipped, not a reason to reopen or to error.
    s1.feed({"contentEnd": None})
    s1.feed(
        {
            "contentStart": {
                "contentId": "c1",
                "role": "ASSISTANT",
                "type": "TEXT",
                "additionalModelFields": "5",
            }
        }
    )
    await _settle(10)
    assert len(harness["streams"]) == 1
    assert not any(e["type"] in ("error", "renewing") for e in harness["events"])
    # Paused delegated turns are dropped when voice mode ends.
    paused_sessions["exec:r1"] = {"messages": []}
    paused_sessions["exec:r2"] = {"messages": []}
    vs.pending.update({"type": "approval_request", "tool_use_id": "t1", "run_id": "r1"})
    vs.pending_queue.append({"type": "approval_request", "tool_use_id": "t2", "run_id": "r2"})
    await vs.stop()
    assert "exec:r1" not in paused_sessions and "exec:r2" not in paused_sessions
    assert vs.pending == {} and vs.pending_queue == []


@run_async
async def test_failed_renewal_still_ends_the_session_cleanly(harness, monkeypatch):
    """stop() runs inside the renew loop when a renewal cannot open a stream; it
    must not cancel itself, or 'ended' is never sent and the browser's mic
    stays hot."""
    monkeypatch.setattr(voice_session, "RENEW_AFTER_S", 5)
    monkeypatch.setattr(voice_session, "RENEW_DEADLINE_S", 6)
    vs = harness["make"]()
    real_opener = vs._open_stream
    opened = {"n": 0}

    async def opener(model_id, region):
        opened["n"] += 1
        if opened["n"] == 1:
            return await real_opener(model_id, region)
        raise RuntimeError("network down")

    vs._open_stream = opener
    await vs.start()
    harness["clock"]["t"] += 10
    for _ in range(60):
        await asyncio.sleep(0.05)
        if vs.done.is_set():
            break
    assert vs.done.is_set()
    assert harness["events"][-1] == {"type": "ended", "reason": "error"}
    assert harness["streams"][0].closed


@run_async
async def test_handler_bug_does_not_cost_the_stream(harness, monkeypatch):
    vs = harness["make"]()
    await vs.start()
    s1 = harness["streams"][0]

    async def broken(ev, live):
        raise RuntimeError("bug")

    monkeypatch.setattr(vs, "_handle", broken)
    s1.feed_text_block("u1", "USER", "hello", "FINAL")
    await _settle(10)
    assert len(harness["streams"]) == 1 and not s1.closed and not vs.done.is_set()
    await vs.stop()


@run_async
async def test_one_response_in_several_speculative_blocks_is_one_utterance(harness):
    """Observed live: Sonic streams a response as one speculative block PER
    SENTENCE, each followed by its audio, then confirms the sentences later.
    That is one utterance, one bubble; the confirmations attach to it, and a
    confirmation arriving after the commit is not a second bubble."""
    vs = harness["make"]()
    await vs.start()
    s = harness["streams"][0]
    s.feed({"completionStart": {}})
    s.feed_text_block("a1", "ASSISTANT", "I've started two agents.", "SPECULATIVE")
    s.feed({"contentStart": {"contentId": "au1", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au1", "content": base64.b64encode(bytes(9600)).decode()}})
    s.feed({"contentEnd": {"contentId": "au1", "stopReason": "END_TURN"}})
    await _settle(5)
    s.feed_text_block("a2", "ASSISTANT", "They may take a few minutes.", "SPECULATIVE")
    s.feed({"contentStart": {"contentId": "au2", "role": "ASSISTANT", "type": "AUDIO"}})
    s.feed({"audioOutput": {"contentId": "au2", "content": base64.b64encode(bytes(9600)).decode()}})
    s.feed({"contentEnd": {"contentId": "au2", "stopReason": "END_TURN"}})
    await _settle(5)
    s.feed_text_block("f1", "ASSISTANT", "I've started two agents.", "FINAL")
    await asyncio.sleep(0.12)
    assert [e for e in _texts(harness["events"]) if e["final"]] == []
    s.feed_text_block("f2", "ASSISTANT", "They may take a few minutes.", "FINAL")
    await asyncio.sleep(0.5)
    finals = [e for e in _texts(harness["events"]) if e["final"]]
    assert [e["text"] for e in finals] == ["I've started two agents. They may take a few minutes."]
    ids = {e["utterance_id"] for e in harness["events"] if e["type"] == "assistant_text"}
    assert len(ids) == 1
    # Straggling confirmations after the commit never become a second bubble.
    s.feed_text_block("f3", "ASSISTANT", "They may take a few minutes.", "FINAL")
    await asyncio.sleep(0.2)
    assert len([e for e in _texts(harness["events"]) if e["final"]]) == 1
    # The next completion is the next utterance.
    s.feed({"completionStart": {}})
    s.feed_text_block("b1", "ASSISTANT", "Anything else?", "SPECULATIVE")
    s.feed({"completionEnd": {"stopReason": "END_TURN"}})
    await _settle(5)
    finals = [e["text"] for e in _texts(harness["events"]) if e["final"]]
    assert finals[-1] == "Anything else?" and len(finals) == 2
    await vs.stop()


@run_async
async def test_renewed_stream_is_told_about_runs_and_pending_decisions(harness, monkeypatch):
    """The transcript seeds the words; the run and pending state must be seeded
    too, or the renewed Sonic answers from imagination (seen live: it said a
    task was running or done while nothing was, and ignored yes/no answers)."""
    monkeypatch.setattr(voice_session, "RENEW_AFTER_S", 10)
    monkeypatch.setattr(voice_session, "RENEW_DEADLINE_S", 20)
    vs = harness["make"]()
    await vs.start()
    vs.runs["r1"] = {"request": "check for bugs", "detached": True}
    vs.pending.update(
        {"type": "approval_request", "tool_use_id": "t1", "run_id": "r1", "summary": "push main"}
    )
    harness["clock"]["t"] += 25
    for _ in range(40):
        await asyncio.sleep(0.05)
        if len(harness["streams"]) == 2:
            break
    s2 = harness["streams"][1]
    seeded = [e["textInput"]["content"] for e in s2.sent if "textInput" in e]
    note = [
        c for c in seeded if c.startswith("[The conversation continues after a connection renewal.")
    ]
    assert len(note) == 1
    assert "check for bugs" in note[0] and "push main" in note[0] and "resolve_request" in note[0]
    # The note sits after the transcript and before the audio block opens.
    kinds = s2.kinds()
    assert kinds[-1] == "contentStart" and s2.sent[-1]["contentStart"]["type"] == "AUDIO"
    # A first stream (no state) gets no note.
    first = [e["textInput"]["content"] for e in harness["streams"][0].sent if "textInput" in e]
    assert not any(c.startswith("[The conversation continues") for c in first)
    await vs.stop()


@run_async
async def test_hang_up_with_a_run_waiting_on_the_card_keeps_it_answerable(harness, monkeypatch):
    """Stopping voice while a run waits for the user's decision does not throw
    the run away: the socket drains, the card stays, a click resolves it, the
    resumed run finishes, then the session ends."""
    resumed = asyncio.Event()

    async def fake_run_tool(name, tool_input, ctx):
        if name == "ask_assistant":
            ctx.pending.update(
                {
                    "type": "approval_request",
                    "tool_use_id": "t1",
                    "run_id": "r1",
                    "summary": "run tests",
                }
            )
            return "The assistant paused: it needs the user's approval to run tests."
        assert name == "resolve_request"
        ctx.pending.clear()

        async def run():
            await asyncio.sleep(0.05)
            await ctx.emit(
                {
                    "type": "assistant_answer",
                    "run_id": "r1",
                    "request": "x",
                    "output": "Tests pass.",
                    "status": "ok",
                }
            )
            resumed.set()

        ctx.register_task(asyncio.create_task(run()))
        return "Working on it: x."

    monkeypatch.setattr(voice_session, "run_tool", fake_run_tool)
    vs = harness["make"]()
    await vs.start()
    harness["streams"][0].feed(
        {"toolUse": {"toolUseId": "tu-1", "toolName": "ask_assistant", "content": "{}"}}
    )
    await _settle(20)
    assert vs.pending
    stop_task = asyncio.create_task(vs.stop("user"))
    await _settle(20)
    types = [e["type"] for e in harness["events"]]
    assert "draining" in types and "ended" not in types and not vs.done.is_set()
    assert vs.pending  # the card is still up
    vs.resolve_pending_from_ui("approve")
    await stop_task
    assert resumed.is_set()
    types = [e["type"] for e in harness["events"]]
    assert types.index("assistant_answer") < types.index("ended")
    assert vs.done.is_set() and vs.pending == {}
