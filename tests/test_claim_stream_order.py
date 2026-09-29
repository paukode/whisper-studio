"""The stream guard keeps the reply in order (server/chat/claim_guard.py).

A tool call's frames wait behind a held sentence only while one of the
round's calls could make it true; a cut, a retried cut and a pause carry the
sentence in progress whole and in order; the calls are kept as first read;
a note is suppressed only by a correction; the memory hooks read the turn
without the dropped sentences.
"""

import asyncio
import json
import time

from server.chat import claim_guard
from server.chat.claim_guard import ClaimGuard
from server.chat.engine.events import (
    Frame,
    Incomplete,
    RoundError,
    RoundResult,
    TextDelta,
    ToolCall,
    ToolCallStart,
)

PROMPT = [{"role": "user", "content": "do the work"}]


async def _aiter(items):
    for item in items:
        yield item


def _wrap(guard, events, messages=None):
    async def go():
        return [ev async for ev in guard.wrap(_aiter(events), messages or PROMPT)]

    return asyncio.run(go())


def _text(items) -> str:
    return "".join(i.text for i in items if isinstance(i, TextDelta))


def _sse_text(frames: list[str]) -> str:
    return "".join(json.loads(f[6:]).get("text", "") for f in frames if f.startswith("data: {"))


def _tool_round(text, name, given, tid="t1"):
    content = [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": tid, "name": name, "input": given},
    ]
    return [
        TextDelta(text=text),
        ToolCallStart(name=name),
        ToolCall(id=tid, name=name, input=given),
        RoundResult(stop_reason="tool_use", content=content),
    ]


def _guard():
    return ClaimGuard(started_at=time.time() - 5)


def test_a_false_claim_no_call_could_make_true_goes_before_the_tools_run():
    guard = _guard()
    text = "Pushed the fix to `main`. Here is what the logs say: the loader times out."
    out = _wrap(guard, _tool_round(text, "ws_run_command", {"command": "make test"}))
    at_result = next(i for i, ev in enumerate(out) if isinstance(ev, RoundResult))
    shown = _text(out[:at_result])
    # The tool card comes after the text, and before the tools run.
    card = next(i for i, ev in enumerate(out) if isinstance(ev, ToolCallStart))
    assert "Here is what the logs say" in shown and "Pushed the fix" not in shown
    assert card < at_result and _text(out[:card]) == shown


def test_a_claim_its_own_call_could_make_true_follows_the_call_as_its_own_paragraph(tmp_path):
    notes = tmp_path / "notes.md"
    guard = _guard()
    out = _wrap(guard, _tool_round(f"Saved the notes to `{notes}`.", "save_file", {}))
    assert str(notes) not in _text(out)
    assert any(isinstance(ev, ToolCallStart) for ev in out)
    notes.write_text("x")
    later = _sse_text(guard.after_tools(PROMPT))
    assert later == f"Saved the notes to `{notes}`.\n\n"


def test_a_retry_after_a_cut_keeps_the_sentence_in_progress(tmp_path):
    missing = tmp_path / "q3.docx"
    guard = _guard()
    _wrap(guard, [TextDelta(text="Done. I saved the report to"), RoundResult("max_tokens", [])])
    retry = _wrap(guard, [RoundError(message="transient", retryable=True)])
    assert _text(retry) == ""
    out = _wrap(
        guard,
        [TextDelta(text=f" `{missing}`. It has three sections."), RoundResult("end_turn", [])],
    )
    assert _text(out) == " It has three sections."
    assert f"Not saved: `{missing}` does not exist." in _sse_text(guard.finish())


def test_a_pause_hands_everything_from_its_first_held_sentence_on_in_order():
    ctx = type(
        "Ctx",
        (),
        {
            "turn_scope_id": None,
            "session_id": "s-order",
            "messages": list(PROMPT),
            "ws_path": "",
            "plan_mode": False,
            "cost_source": "chat",
        },
    )()
    guard = ClaimGuard.for_turn(ctx)
    out = _wrap(
        guard,
        _tool_round(
            "Pushed the fix to `main`. The CI will pick it up.", "git_push", {"branch": "main"}
        ),
    )
    assert "CI will pick it up" not in _text(out)
    assert _sse_text(guard.finish(paused=True)) == ""
    approved = [
        *PROMPT,
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "git_push", "input": {"branch": "main"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "To github.com:a/b.git\n   abc..def  main -> main",
                }
            ],
        },
    ]
    ctx.messages = approved
    resumed = ClaimGuard.for_turn(ctx)
    out = _wrap(
        resumed, [TextDelta(text="The push went through."), RoundResult("end_turn", [])], approved
    )
    assert (
        _text(out) == "Pushed the fix to `main`. The CI will pick it up.\n\nThe push went through."
    )


def test_a_pause_carries_its_dropped_claims_to_the_continuations_notes():
    guard = _guard()
    guard.session_id = "s-drops"
    _wrap(
        guard,
        [TextDelta(text="I emailed the notes to dana@acme.com."), RoundResult("end_turn", [])],
    )
    assert _sse_text(guard.finish(paused=True)) == ""
    paused = claim_guard._PAUSED_STARTS.pop("s-drops")
    resumed = ClaimGuard(started_at=paused.started_at, paused=paused)
    _wrap(resumed, [TextDelta(text="Understood."), RoundResult("end_turn", [])])
    assert "Not sent" in _sse_text(resumed.finish())


def test_an_unclaimed_pause_expires():
    claim_guard._PAUSED_STARTS["s-old"] = claim_guard._Paused(
        1.0, "q", [], stamp=time.monotonic() - 7200
    )
    claim_guard._expire_paused()
    assert "s-old" not in claim_guard._PAUSED_STARTS


def test_a_call_is_kept_as_first_read_when_compaction_shortens_its_result():
    guard = _guard()
    failed = (
        "To github.com:a/b.git\n"
        + "x" * 2100
        + "\n! [rejected] main -> main\nerror: failed to push\n(exit code 1)"
    )
    call = {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "c1",
                "name": "ws_run_command",
                "input": {"command": "git push origin main"},
            }
        ],
    }
    full = [
        *PROMPT,
        call,
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": failed}],
        },
    ]
    guard.after_tools(full)
    pruned = [
        *PROMPT,
        call,
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c1",
                    "content": failed[:2000] + "\n...[truncated]",
                }
            ],
        },
    ]
    out = _wrap(
        guard,
        [TextDelta(text="Pushed the fix to `main`. Anything else?"), RoundResult("end_turn", [])],
        pruned,
    )
    assert _text(out) == "Anything else?"
    assert not guard.ledger()[0].ok


def test_an_output_cap_notice_waits_behind_a_held_sentence(tmp_path):
    missing = tmp_path / "q3.docx"
    guard = _guard()
    out = _wrap(
        guard,
        [
            TextDelta(text=f"I saved the report to {missing}. More text."),
            Incomplete(),
            RoundResult("end_turn", [{"type": "text", "text": "x"}]),
        ],
    )
    # The notice comes once the held sentence is settled, never ahead of it.
    order = [type(ev).__name__ for ev in out if isinstance(ev, (TextDelta, Incomplete))]
    assert order.index("Incomplete") > order.index("TextDelta")
    assert str(missing) not in _text(out)


def test_only_a_correction_keeps_the_note_away(tmp_path):
    missing = tmp_path / "q3.pptx"
    for later, noted in (
        (" Open `q3.pptx` in Keynote to review it.", True),
        (" Sorry, I could not create `q3.pptx`.", False),
    ):
        guard = _guard()
        _wrap(
            guard,
            [TextDelta(text=f"I saved the deck to {missing}.{later}"), RoundResult("end_turn", [])],
        )
        assert ("Not saved" in _sse_text(guard.finish())) is noted, later


def test_the_memory_hooks_read_the_turn_without_the_dropped_sentence(tmp_path):
    missing = tmp_path / "q3.docx"
    guard = _guard()
    reply = f"I saved the report to {missing}. The totals are up 4%."
    _wrap(guard, [TextDelta(text=reply), RoundResult("end_turn", [])])
    rows = [*PROMPT, {"role": "assistant", "content": [{"type": "text", "text": reply}]}]
    seen = guard.redact(rows)
    assert "saved the report" not in seen[-1]["content"][0]["text"]
    assert "totals are up" in seen[-1]["content"][0]["text"]
    assert rows[-1]["content"][0]["text"] == reply


def test_a_long_paragraph_with_no_sentence_end_streams_in_linear_time():
    guard = _guard()
    para = "word " * 16000
    started = time.perf_counter()
    _wrap(
        guard,
        [TextDelta(text=para[i : i + 40]) for i in range(0, len(para), 40)]
        + [RoundResult("end_turn", [])],
    )
    assert time.perf_counter() - started < 3.0


def test_a_title_keeps_its_sentence_whole(tmp_path):
    guard = _guard()
    out = _wrap(
        guard,
        [
            TextDelta(text="I emailed the report to Dr. Smith this morning. Bye."),
            RoundResult("end_turn", []),
        ],
    )
    assert _text(out) == "Bye."
    assert "Smith" in _sse_text(guard.finish())


def test_a_verified_delivery_is_announced_once(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("x")
    guard = _guard()
    out = _wrap(
        guard,
        [TextDelta(text=f"Saved {report}. Saved {report} again."), RoundResult("end_turn", [])],
    )
    chips = [r for ev in out if isinstance(ev, Frame) for r in ev.payload["deliveries"]["items"]]
    assert len(chips) == 1


def test_a_list_after_a_blank_line_under_its_lead_is_held():
    guard = _guard()
    text = "I saved these files:\n\n- `/Users/nobody-guard/a.md`\n- `/Users/nobody-guard/b.md`\n"
    out = _wrap(
        guard,
        [TextDelta(text=text[i : i + 7]) for i in range(0, len(text), 7)]
        + [RoundResult("end_turn", [])],
    )
    assert "a.md" not in _text(out) and "b.md" not in _text(out)
    notes = _sse_text(guard.finish())
    assert "a.md` does not exist" in notes and "b.md` does not exist" in notes


def test_a_claim_deep_inside_a_long_code_block_is_code():
    guard = _guard()
    body = "\n".join(f"line {i} of the example output" for i in range(200))
    text = f"Output:\n```text\n{body}\nI pushed the fix to `main`.\n```\nThat is all.\n"
    out = _wrap(
        guard,
        [TextDelta(text=text[i : i + 50]) for i in range(0, len(text), 50)]
        + [RoundResult("end_turn", [])],
    )
    assert "I pushed the fix to `main`." in _text(out)
    assert _sse_text(guard.finish()) == ""
