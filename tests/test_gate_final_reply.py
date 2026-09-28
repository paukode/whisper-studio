"""The completion gate checks the reply it is gating.

The runner hands the gate the history and, apart from it, the reply being
gated (``GateContext.final_reply``): the reply is persisted only after the
decision. The judge read it from there, but the deliverable, requested-file
and verification checks read ``messages`` alone, so the "last assistant row"
they checked was the round before the reply, usually a tool round's preamble.
A final reply saying "Saved to `/path/report.docx`" or "the artifact card
above" was never checked. Every context here is built the way the runner
builds it, with the reply only in ``final_reply``.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.goals import GateContext, Verdict, gate
from server.goals import store as goal_store
from server.goals.requested_files import REQUEST_MARKER


def _reply(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def _history(prompt: str, tool: str = "run_python") -> list[dict]:
    """A prompt and one tool round, so the row before the reply is a tool
    round's preamble, as in a real turn."""
    return [
        {"role": "user", "content": prompt},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "t1", "name": tool, "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]


def _decide(history: list, reply: list):
    ctx = GateContext(session_id="s", messages=history, final_reply=reply)
    return asyncio.run(gate.run_completion_gate(ctx))


@pytest.fixture
def gate_flags(monkeypatch):
    """Turn on only the named gate flags, with the Stop hooks quiet."""
    from server import hooks

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)

    def only(*names: str) -> None:
        monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name in names)

    return only


# ── the gate ───────────────────────────────────────────────────────────────


def test_a_reply_claiming_a_file_that_was_never_written_is_held(gate_flags, tmp_path):
    gate_flags("deliverable_check")
    history = _history("write the report as a docx")
    claim = _reply(f"Saved to `{tmp_path}/report.docx`.")
    held = _decide(history, claim)
    assert held.block and held.source == "deliverable"
    assert "report.docx" in held.feedback

    (tmp_path / "report.docx").write_bytes(b"content")
    assert _decide(history, claim).block is False


def test_a_reply_pointing_at_an_artifact_card_nobody_made_is_held(gate_flags):
    gate_flags("deliverable_check")
    reply = _reply("Done. The app is available in the artifact card above.")
    held = _decide(_history("build me a timer app"), reply)
    assert held.block and held.source == "deliverable"
    assert "create_artifact" in held.feedback

    made = _history("build me a timer app", tool="create_artifact")
    assert _decide(made, reply).block is False


def test_a_reply_naming_the_file_a_script_wrote_answers_the_request(gate_flags, tmp_path):
    # The skill path: a script writes the document, no file tool runs, and
    # only the path in the reply shows the file was produced.
    gate_flags("requested_file_check")
    history = _history("write the report as a docx and save it to Downloads")
    out = tmp_path / "report.docx"
    out.write_bytes(b"content")
    assert _decide(history, _reply(f"Saved to `{out}`.")).block is False

    nudged = _decide(history, _reply(f"Saved to `{tmp_path}/ghost.docx`."))
    assert nudged.block and nudged.source == "requested_file"
    assert nudged.feedback.startswith(REQUEST_MARKER)


def test_the_checks_and_the_judge_read_one_transcript(gate_flags, monkeypatch):
    from server.goals import deliverables, evaluator, requested_files, verification

    seen: dict[str, list] = {}

    def spy(name):
        def check(messages, *a, **k):
            seen[name] = messages

        return check

    def judge(goal, messages, **k):
        seen["judge"] = messages
        return Verdict("achieved", "done", 0.9)

    monkeypatch.setattr(deliverables, "check_claims", spy("claims"))
    monkeypatch.setattr(requested_files, "requested_file_feedback", spy("requested_file"))
    monkeypatch.setattr(verification, "verify_on_stop_feedback", spy("verify"))
    monkeypatch.setattr(evaluator, "evaluate", judge)
    gate_flags("deliverable_check", "requested_file_check", "verify_on_stop", "goal_loop")
    sid = "gate-one-transcript"
    goal_store.set_goal(sid, "ship the report")

    history = _history("write the report as a docx")
    reply = _reply("Saved to `/nowhere/report.docx`.")
    ctx = GateContext(session_id=sid, goal="ship the report", messages=history, final_reply=reply)
    assert asyncio.run(gate.run_completion_gate(ctx)).goal_achieved

    gated = [*history, {"role": "assistant", "content": reply}]
    assert seen == {"claims": gated, "requested_file": gated, "verify": gated, "judge": gated}
    # Checked on a copy: the runner's own list is left as it was.
    assert ctx.messages == _history("write the report as a docx")


# ── the engine ─────────────────────────────────────────────────────────────
# A bare TurnContext and a scripted adapter, the pattern from
# tests/test_midturn_late_message.py: no HTTP route, no real Bedrock.


class _ScriptTurn:
    """Round 0 runs a script; each later round answers with the next reply."""

    provider = "test"

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == 0:
            content = [
                {"type": "text", "text": "I'll write the report with a script."},
                {"type": "tool_use", "id": "t1", "name": "run_python", "input": {"code": "..."}},
            ]
            yield TextDelta(text=content[0]["text"])
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        text = self.replies.pop(0)
        yield TextDelta(text=text)
        yield RoundResult(stop_reason="end_turn", content=_reply(text), usage=Usage())


def _fake_tools(monkeypatch):
    import server.tool_executor as TE

    async def batch(tool_uses, **kw):
        return list(tool_uses)

    async def process(states, budget_fn, **kw):
        results = [{"type": "tool_result", "tool_use_id": t["id"], "content": "ok"} for t in states]
        return (results, [], False, False)

    monkeypatch.setattr(TE, "execute_tool_batch", batch)
    monkeypatch.setattr(TE, "process_tool_results", process)


def _run(sid: str, adapter) -> str:
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "write the quarterly report as a docx"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=10, completion_gate=True),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return "".join([c async for c in run_turn(ctx)])

    return asyncio.run(go())


def test_the_engine_holds_a_final_reply_that_claims_a_missing_file(
    gate_flags, monkeypatch, tmp_path
):
    gate_flags("deliverable_check")
    _fake_tools(monkeypatch)
    claim = f"Saved to `{tmp_path}/report.docx`."
    adapter = _ScriptTurn(claim, "The report was not saved: the script never wrote it.")
    out = _run("gate-final-reply-held", adapter)

    # The script round, the claim, then the round the gate held the turn for.
    assert len(adapter.calls) == 3
    held, feedback = adapter.calls[2][-2:]
    assert held == {"role": "assistant", "content": _reply(claim)}
    assert feedback["role"] == "user"
    assert feedback["content"].startswith("[completion gate]")
    assert "report.docx" in feedback["content"]
    assert "stop_hook_block" in out

    # Once the file is really there, the same reply ends the turn.
    (tmp_path / "report.docx").write_bytes(b"content")
    adapter = _ScriptTurn(claim)
    out = _run("gate-final-reply-written", adapter)
    assert len(adapter.calls) == 2
    assert "stop_hook_block" not in out
