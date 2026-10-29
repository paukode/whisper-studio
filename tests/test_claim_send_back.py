"""A held claim goes back to the model or agent that wrote it, with no card.

server/chat/claim_guard.py holds a sentence that claims a delivery, and the
text after it goes on at once. A claim that does not hold goes back to the
model: with a tool round's results (``send_back``), through the completion
gate for a chat reply, and through the runner where no gate reads the reply
(agents, scheduled and headless runs, on-device chat with no goal). The chat
shows no card for any of it. The server's note says what did not happen only
when the model neither made the claim true nor corrected it.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from server.chat.claim_guard import ClaimGuard
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.goals import gate
from server.goals.deliverables import CLAIM_MARKER

_PROMPT = [{"role": "user", "content": "write the notes"}]


async def _aiter(items):
    for item in items:
        yield item


def _wrap(guard, events, messages=None):
    async def go():
        return [ev async for ev in guard.wrap(_aiter(events), messages or _PROMPT)]

    return asyncio.run(go())


def _text(items) -> str:
    return "".join(i.text for i in items if isinstance(i, TextDelta))


def _guard() -> ClaimGuard:
    return ClaimGuard(started_at=time.time() - 5)


# ── the guard on its own ─────────────────────────────────────────────────


def test_the_text_after_a_held_claim_goes_out_before_the_round_ends(tmp_path):
    missing = tmp_path / "app.dmg"
    guard = _guard()
    out = _wrap(
        guard,
        [
            TextDelta(text=f"I saved the installer to {missing}. "),
            TextDelta(text="Open it from Finder to install. "),
            TextDelta(text="Then restart"),
        ],
    )
    # No round result yet: the claim is held, the next sentence is not.
    assert "Open it from Finder to install." in _text(out)
    assert str(missing) not in _text(out)


def test_a_dropped_claim_is_sent_back_once(tmp_path):
    missing = tmp_path / "notes.md"
    guard = _guard()
    _wrap(guard, [TextDelta(text=f"I saved the notes to {missing}."), RoundResult("end_turn", [])])
    note = guard.send_back()
    assert note.startswith(CLAIM_MARKER) and str(missing) in note
    assert guard.send_back() is None
    # Told once, and never corrected: the turn still ends with the truth.
    assert "Not saved" in json.loads(guard.finish()[0][6:])["text"]


def test_the_send_back_shares_the_turns_claim_nudges(tmp_path):
    missing = tmp_path / "notes.md"
    nudge = {"role": "user", "content": f"[completion gate] {CLAIM_MARKER} earlier"}
    acted = {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "x"}]}
    used_up = [*_PROMPT, nudge, acted, nudge, acted]
    guard = _guard()
    _wrap(
        guard,
        [TextDelta(text=f"I saved the notes to {missing}."), RoundResult("end_turn", [])],
        used_up,
    )
    assert guard.send_back() is None


# ── the engine ───────────────────────────────────────────────────────────


class _Script:
    """Each round plays the next step: ("say", text) ends the turn, ("call",
    text) says text and calls web_search, a tool that writes no file."""

    provider = "test"

    def __init__(self, *steps, tools_enabled: bool = True):
        self.steps = list(steps)
        self.calls: list[list[dict]] = []
        self.tools_enabled = tools_enabled

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        kind, text = self.steps.pop(0)
        content = [{"type": "text", "text": text}]
        for i in range(0, len(text), 9):
            yield TextDelta(text=text[i : i + 9])
        if kind == "call":
            content.append({"type": "tool_use", "id": f"t{round_num}", "name": "web_search"})
            content[-1]["input"] = {"query": "release notes"}
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        yield RoundResult(stop_reason="end_turn", content=content, usage=Usage())


@pytest.fixture
def quiet(monkeypatch):
    """Only the claim check on, the Stop hooks quiet, and tools that answer
    "ok" without running."""
    import server.tool_executor as TE
    from server import hooks

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    async def batch(tool_uses, **kw):
        return list(tool_uses)

    async def process(states, budget_fn, **kw):
        results = [{"type": "tool_result", "tool_use_id": t["id"], "content": "ok"} for t in states]
        return (results, [], False, False)

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "deliverable_check")
    monkeypatch.setattr(TE, "execute_tool_batch", batch)
    monkeypatch.setattr(TE, "process_tool_results", process)


def _run(sid: str, adapter, policy: TurnPolicy) -> list[dict]:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=list(_PROMPT),
        adapter=adapter,
        policy=policy,
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


def _said(frames) -> str:
    return "".join(f["text"] for f in frames if isinstance(f.get("text"), str))


def _no_card(frames) -> bool:
    return not any("stop_hook_block" in f for f in frames)


_CHAT = TurnPolicy(max_rounds=8)
_AGENT = TurnPolicy(max_rounds=8, completion_gate=False)
_ON_DEVICE = TurnPolicy(max_rounds=8, gate_requires_goal=True)


def test_a_claim_in_a_tool_round_goes_back_with_the_results(quiet, tmp_path):
    missing = tmp_path / "notes.md"
    adapter = _Script(
        ("call", f"I saved the notes to {missing}. Now the release notes."),
        ("say", "notes.md was not saved: I have not written it yet."),
    )
    frames = _run("send-back-tool-round", adapter, _CHAT)
    results = adapter.calls[1][-1]
    assert results["content"][0]["type"] == "tool_result"
    note = results["content"][-1]["text"]
    assert note.startswith(f"[completion gate] {CLAIM_MARKER}") and str(missing) in note
    said = _said(frames)
    assert "Now the release notes." in said and str(missing) not in said
    assert "Not saved" not in said and _no_card(frames)
    assert len(adapter.calls) == 2


def test_a_chat_reply_is_sent_back_quietly_and_the_answer_starts_a_paragraph(quiet, tmp_path):
    missing = tmp_path / "notes.md"
    adapter = _Script(
        ("say", f"Here is the summary. I saved it to {missing}."),
        ("say", "I could not write notes.md: the folder is read-only."),
    )
    frames = _run("send-back-chat", adapter, _CHAT)
    said = _said(frames)
    assert said.startswith("Here is the summary.")
    assert "\n\nI could not write notes.md" in said and str(missing) not in said
    assert _no_card(frames) and len(adapter.calls) == 2


@pytest.mark.parametrize("policy", [_AGENT, _ON_DEVICE], ids=["agent", "on-device"])
def test_a_run_no_gate_reads_sends_the_claim_back_itself(quiet, tmp_path, policy):
    missing = tmp_path / "out.csv"
    adapter = _Script(
        ("say", f"Exported the table to {missing}."),
        ("say", "out.csv was not written: the export failed."),
    )
    frames = _run(f"send-back-{id(policy)}", adapter, policy)
    feedback = adapter.calls[1][-1]
    assert feedback["content"].startswith(f"[completion gate] {CLAIM_MARKER}")
    said = _said(frames)
    assert "Exported the table" not in said and "Not saved" not in said
    assert _no_card(frames) and len(adapter.calls) == 2


def test_an_agent_that_insists_gets_the_note_after_one_send_back(quiet, tmp_path):
    missing = tmp_path / "out.csv"
    adapter = _Script(
        ("say", f"Exported the table to {missing}."),
        ("say", f"Exported the table to {missing}."),
    )
    frames = _run("send-back-insists", adapter, _AGENT)
    said = _said(frames)
    assert "Exported the table" not in said
    assert f"Not saved: `{missing}` does not exist." in said
    assert len(adapter.calls) == 2


def test_a_model_with_no_tools_is_not_sent_back(quiet, tmp_path):
    missing = tmp_path / "out.csv"
    adapter = _Script(("say", f"Exported the table to {missing}."), tools_enabled=False)
    frames = _run("send-back-no-tools", adapter, _AGENT)
    assert f"Not saved: `{missing}` does not exist." in _said(frames)
    assert len(adapter.calls) == 1


def test_a_held_claim_a_call_makes_true_follows_the_text_after_it(tmp_path):
    notes = tmp_path / "notes.md"
    guard = _guard()
    text = f"Saved the notes to {notes}. Then I check the data."
    content = [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": "t1", "name": "save_file", "input": {}},
    ]
    out = _wrap(guard, [TextDelta(text=text), RoundResult("tool_use", content)])
    assert _text(out) == "Then I check the data."
    notes.write_text("x")
    later = "".join(json.loads(f[6:]).get("text", "") for f in guard.after_tools(_PROMPT))
    assert later == f"\n\nSaved the notes to {notes}.\n\n"
