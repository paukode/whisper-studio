"""The stream guard: a claimed delivery reaches the user only once verified.

server/chat/claim_guard.py holds the sentence that makes a claim until
server/goals/evidence.py has checked it. A verified claim is released with a
``deliveries`` receipt; a false one is never sent, and the turn ends with the
server's own note on what did not happen unless the model corrected itself.
"""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from server.chat import claim_guard
from server.chat.claim_guard import ClaimGuard
from server.chat.engine.events import Frame, RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.goals import gate
from server.goals.gate import CLAIM_HELD_REASON


async def _aiter(items):
    for item in items:
        yield item


def _round(guard: ClaimGuard, deltas: list[str], messages: list, *, tool_call: bool = False):
    content = [{"type": "text", "text": "".join(deltas)}]
    if tool_call:
        content.append({"type": "tool_use", "id": "t1", "name": "save_file", "input": {}})
    events = [TextDelta(text=d) for d in deltas]
    events.append(RoundResult(stop_reason="tool_use" if tool_call else "end_turn", content=content))

    async def go():
        return [ev async for ev in guard.wrap(_aiter(events), messages)]

    return asyncio.run(go())


def _text(items) -> str:
    return "".join(i.text for i in items if isinstance(i, TextDelta))


def _receipts(items) -> list[dict]:
    return [r for i in items if isinstance(i, Frame) for r in i.payload["deliveries"]["items"]]


def _frames_text(frames: list[str]) -> str:
    out = []
    for f in frames:
        payload = json.loads(f[len("data: ") :])
        if "text" in payload:
            out.append(payload["text"])
    return "".join(out)


def _guard() -> ClaimGuard:
    return ClaimGuard(started_at=time.time() - 5)


_PROMPT = [{"role": "user", "content": "write the report"}]


# ── the guard on its own ─────────────────────────────────────────────────


def test_a_verified_claim_is_shown_with_its_receipt(tmp_path):
    report = tmp_path / "report.html"
    report.write_text("<p>q3</p>")
    guard = _guard()
    items = _round(guard, ["I saved the report to ", f"{report}", ". Anything else?"], _PROMPT)
    assert _text(items) == f"I saved the report to {report}. Anything else?"
    (receipt,) = _receipts(items)
    assert receipt["kind"] == "file" and receipt["target"] == str(report)
    assert guard.finish() == []


def test_a_false_claim_is_never_sent_and_the_turn_ends_with_the_truth(tmp_path):
    missing = tmp_path / "report.docx"
    guard = _guard()
    items = _round(
        guard, ["Here is the summary. ", f"I saved it to {missing}", ". Anything else?"], _PROMPT
    )
    shown = _text(items)
    assert "Here is the summary." in shown and "Anything else?" in shown
    assert str(missing) not in shown and "saved it" not in shown
    note = _frames_text(guard.finish())
    assert note.strip() == f"*Not saved: `{missing}` does not exist.*"


def test_text_with_no_sign_of_a_claim_streams_as_it_comes():
    guard = _guard()
    words = ["Thinking about it more, the main risk is timing and the cost of waiting too long. "]
    words += ["word " * 40]
    events = [TextDelta(text=w) for w in words]

    async def first_two():
        out = []
        async for ev in guard.wrap(_aiter(events), _PROMPT):
            out.append(ev)
        return out

    items = asyncio.run(first_two())
    # Both deltas were answered before any round result arrived.
    assert len([i for i in items if isinstance(i, TextDelta)]) >= 2


def test_a_sentence_that_names_a_path_is_released_whole(tmp_path):
    report = tmp_path / "q3.md"
    report.write_text("x")
    guard = _guard()
    pieces = ["I ", "saved ", "the ", "notes ", "to ", f"{report}", ".", " Done."]
    items = _round(guard, pieces, _PROMPT)
    sentences = [i.text for i in items if isinstance(i, TextDelta)]
    assert f"I saved the notes to {report}." in sentences[0]


def test_a_claim_ahead_of_the_call_that_makes_it_true_waits_for_the_call(tmp_path):
    notes = tmp_path / "notes.md"
    guard = _guard()
    items = _round(guard, [f"Saved the notes to {notes}."], _PROMPT, tool_call=True)
    assert str(notes) not in _text(items)
    notes.write_text("x")
    frames = guard.after_tools(_PROMPT)
    assert f"Saved the notes to {notes}." in _frames_text(frames)
    assert guard.finish() == []


def test_a_claim_still_false_after_the_calls_is_dropped(tmp_path):
    notes = tmp_path / "notes.md"
    guard = _guard()
    _round(guard, [f"Saved the notes to {notes}. Next I check the data."], _PROMPT, tool_call=True)
    shown = _frames_text(guard.after_tools(_PROMPT))
    assert "Next I check the data." in shown and str(notes) not in shown
    assert "Not saved" in _frames_text(guard.finish())


def test_a_correction_that_names_the_target_needs_no_note(tmp_path):
    missing = tmp_path / "report.docx"
    guard = _guard()
    _round(guard, [f"Saved to {missing}."], _PROMPT)
    _round(guard, ["I could not write report.docx: the folder is read-only."], _PROMPT)
    assert guard.finish() == []


def test_a_claim_inside_a_code_block_is_not_held():
    guard = _guard()
    text = "Run this:\n```\ngit push origin main\nSaved to ~/Downloads/nowhere.docx\n```\n"
    assert _text(_round(guard, [text], _PROMPT)) == text
    assert guard.finish() == []


def test_a_retried_attempt_discards_what_it_held(tmp_path):
    missing = tmp_path / "gone.docx"
    guard = _guard()

    async def aborted():
        return [
            ev async for ev in guard.wrap(_aiter([TextDelta(text=f"Saved to {missing}")]), _PROMPT)
        ]

    assert _text(asyncio.run(aborted())) == ""
    items = _round(guard, ["Here is the answer."], _PROMPT)
    assert _text(items) == "Here is the answer."
    assert guard.finish() == []


def test_a_paused_turn_notes_nothing_and_its_continuation_keeps_its_start(tmp_path):
    missing = tmp_path / "upload.csv"
    ctx = SimpleNamespace(
        turn_scope_id=None,
        session_id="s-pause",
        messages=list(_PROMPT),
        ws_path="",
        plan_mode=False,
        cost_source="chat",
    )
    guard = ClaimGuard.for_turn(ctx)
    _round(guard, [f"Uploaded {missing} to s3://bucket/upload.csv."], _PROMPT)
    assert _frames_text(guard.finish(paused=True)) == ""
    resumed = ClaimGuard.for_turn(ctx)
    assert resumed.started_at == guard.started_at
    # A new turn with another prompt starts its own clock.
    claim_guard._PAUSED_STARTS["s-pause"] = claim_guard._Paused(1.0, "something else", [])
    assert ClaimGuard.for_turn(ctx).started_at != 1.0


def test_the_memory_agents_notes_are_not_guarded(tmp_path):
    ctx = SimpleNamespace(
        turn_scope_id="agent:m1",
        session_id="s-mem",
        messages=list(_PROMPT),
        ws_path="",
        plan_mode=False,
        cost_source="memory",
    )
    guard = ClaimGuard.for_turn(ctx)
    line = f"- Saved the Q3 report to {tmp_path / 'gone.docx'} (turn 4)."
    assert _text(_round(guard, [line], _PROMPT)) == line
    assert guard.finish() == []


# ── the guard in the engine ──────────────────────────────────────────────


class _Replies:
    provider = "test"

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls = 0

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls += 1
        text = self.replies.pop(0)
        for i in range(0, len(text), 7):
            yield TextDelta(text=text[i : i + 7])
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


@pytest.fixture
def claims_only(monkeypatch):
    from server import hooks

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "deliverable_check")


def _run(sid: str, adapter, *, completion_gate: bool = True) -> list[dict]:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "summarize the meeting"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=12, completion_gate=completion_gate),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return "".join([c async for c in run_turn(ctx)])

    frames = []
    for line in asyncio.run(go()).splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            frames.append(json.loads(line[len("data: ") :]))
    return frames


def _said(frames: list[dict]) -> str:
    return "".join(f["text"] for f in frames if isinstance(f.get("text"), str))


def test_the_engine_never_shows_a_false_claim_and_sends_the_model_back(claims_only, tmp_path):
    missing = tmp_path / "report.docx"
    adapter = _Replies(
        f"Done. Saved the report to {missing}.",
        f"I could not write {missing.name}: the folder is read-only.",
    )
    frames = _run("guard-engine", adapter)
    said = _said(frames)
    assert "Saved the report" not in said and str(missing) not in said
    assert "could not write report.docx" in said and "Not saved" not in said
    (block,) = [f["stop_hook_block"] for f in frames if "stop_hook_block" in f]
    assert block["source"] == "deliverable" and block["reason"] == CLAIM_HELD_REASON
    assert adapter.calls == 2


def test_a_run_with_no_gate_says_what_did_not_happen(tmp_path):
    missing = tmp_path / "out.csv"
    frames = _run(
        "guard-agent", _Replies(f"Exported the table to {missing}."), completion_gate=False
    )
    said = _said(frames)
    assert "Exported the table" not in said
    assert f"Not saved: `{missing}` does not exist." in said


def test_the_engine_sends_a_receipt_for_what_it_verified(claims_only, tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    os.utime(report, None)
    frames = _run("guard-receipt", _Replies(f"Saved the report to {report}."))
    assert f"Saved the report to {report}." in _said(frames)
    (delivered,) = [f["deliveries"] for f in frames if "deliveries" in f]
    assert delivered["items"][0]["target"] == str(report)


def test_a_correction_after_an_attempt_is_not_nudged_again(tmp_path):
    # Seen live: the model tried an upload the user denied, then said so;
    # the original claim must not draw a second nudge over the correction.
    from server.goals.deliverables import check_claims

    claim = "I uploaded the notes to s3://claim-e2e-bucket/notes.md."
    history = [
        {"role": "user", "content": "upload the notes"},
        {"role": "assistant", "content": [{"type": "text", "text": claim}]},
    ]
    first = check_claims(history, None)
    assert first and "s3://claim-e2e-bucket/notes.md" in first
    history += [
        {"role": "user", "content": f"[completion gate] {first}"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "aws_cli", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[User denied] x"}],
        },
    ]
    correction = "The note was not uploaded to s3://claim-e2e-bucket/notes.md: the copy was denied."
    assert check_claims([*history, _say(correction)], None) is None
    # The claim made again is checked where it is made, and draws the second nudge.
    assert check_claims([*history, _say(claim)], None) is not None


def _say(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def test_the_deliverable_check_flag_turns_the_guard_off(monkeypatch):
    # One control: the flag that stops the gate's claim check stops the hold.
    from server.infrastructure import feature_flags

    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: name != "deliverable_check")
    ctx = SimpleNamespace(
        turn_scope_id=None,
        session_id="s-flag",
        messages=list(_PROMPT),
        ws_path="",
        plan_mode=False,
        cost_source="chat",
    )
    assert not ClaimGuard.for_turn(ctx).enabled
