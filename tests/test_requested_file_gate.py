"""server.goals.requested_files: the user asked for a file and the turn made none.

The mirror of the claimed-deliverable check. Real session: "i want the visual
and ci file i shared at the start to update and save a new version in
downloads" was answered with a revised description in chat, no file written,
and the turn ended. The user learned it only by asking afterwards.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.chat.engine import midturn_inbox
from server.chat.engine.events import RoundResult, TextDelta, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, _midturn_text, _remind, run_turn
from server.goals import requested_files as rf


def _tool(name: str, tool_input: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input or {}}],
    }


# ── reading the ask out of the prompt ──────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "ok, i want the visual and ci file i shared at the start to update and "
        "save a new version in downloads.",
        "dont touch anything on gitlab, generate a local ci yml and a png on flow locally",
        "can you make me a pdf of this?",
        "export the numbers as a csv",
        "update gitlab-ci.yml with the new stage",
        "give me a diagram of the pipeline",
    ],
)
def test_a_request_for_a_file_is_recognised(prompt):
    assert rf.requested_clauses(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "did you update the png? and what file did you store it in?",
        "read the pdf I attached and tell me what it says",
        "explain the yml in the repo",
        "review both pipelines and compare them with the attached flow",
        "what does this csv column mean",
        "summarize the report",
    ],
)
def test_a_question_about_files_is_not_a_request_for_one(prompt):
    assert rf.requested_clauses(prompt) == []


def test_the_verb_and_the_noun_have_to_be_in_the_same_breath():
    # Reading one file then answering in chat owes the user nothing on disk.
    assert rf.requested_clauses("read the pdf. then tell me what changed") == []
    assert rf.requested_clauses("read the pdf, then write me a docx")


def test_every_request_in_a_message_is_read():
    asked = rf.requested_clauses("make a diagram of the flow. Also save a pdf copy to Downloads")
    assert asked == ["make a diagram of the flow", "Also save a pdf copy to Downloads"]


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("no need to save a file, just answer here", False),
        ("don't write the pdf after all", False),
        ("please do not create a file for this", False),
        ("stop exporting the csv", False),
        ("don't forget to save it as a pdf", True),
        ("don't save anything, just make me a diagram", True),
    ],
)
def test_a_file_called_off_is_not_asked_for(text, asks):
    assert bool(rf.requested_clauses(text)) is asks


# ── did the turn actually produce something ────────────────────────────────


@pytest.mark.parametrize(
    "name", ["save_file", "ws_write_file", "ws_create_file", "create_artifact", "create_visual"]
)
def test_a_file_tool_counts_as_produced(name):
    msgs = [{"role": "user", "content": "make me a png"}, _tool(name)]
    assert rf.produced_a_file(msgs, None) is True


def test_a_reply_naming_a_file_that_really_exists_counts_as_produced(tmp_path):
    # The skill path writes documents with a script, not a file tool.
    real = tmp_path / "flow.png"
    real.write_bytes(b"not really a png, but non-empty")
    msgs = [
        {"role": "user", "content": "make me a png"},
        _tool("run_python"),
        {"role": "assistant", "content": f"Saved to `{real}`."},
    ]
    assert rf.produced_a_file(msgs, None) is True


def test_a_reply_naming_a_file_that_does_not_exist_does_not_count(tmp_path):
    msgs = [
        {"role": "user", "content": "make me a png"},
        {"role": "assistant", "content": f"Saved to `{tmp_path}/ghost.png`."},
    ]
    assert rf.produced_a_file(msgs, None) is False


# ── the feedback itself ────────────────────────────────────────────────────


def test_feedback_names_the_ask_when_nothing_was_produced():
    msgs = [
        {"role": "user", "content": "update the diagram and save a new version in downloads"},
        {"role": "assistant", "content": "Here is the revised description of the flow."},
    ]
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and fb.startswith(rf.REQUEST_MARKER)
    assert "save a new version in downloads" in fb


def test_no_feedback_once_the_file_exists(tmp_path):
    out = tmp_path / "flow.png"
    out.write_text("x")
    msgs = [
        {"role": "user", "content": "update the diagram and save it to downloads"},
        _tool("save_file", {"destination_path": str(out)}),
        {"role": "assistant", "content": f"Saved to `{out}`."},
    ]
    assert rf.requested_file_feedback(msgs, None) is None


def test_a_chat_artifact_does_not_satisfy_a_request_to_save_somewhere():
    # "save it in downloads" is not answered by a card in the chat: the user
    # asked for a file they can open, which is the miss that started this.
    asked_for_disk = [
        {"role": "user", "content": "update the diagram and save a new version in downloads"},
        _tool("create_artifact"),
        {"role": "assistant", "content": "Here is the updated diagram."},
    ]
    assert rf.requested_file_feedback(asked_for_disk, None) is not None

    # With no location named, a card is a real answer to "make me a diagram".
    asked_for_anything = [
        {"role": "user", "content": "make me a diagram of the pipeline"},
        _tool("create_artifact"),
        {"role": "assistant", "content": "Here it is."},
    ]
    assert rf.requested_file_feedback(asked_for_anything, None) is None


def test_a_written_file_satisfies_a_request_to_save_somewhere():
    msgs = [
        {"role": "user", "content": "update the diagram and save a new version in downloads"},
        _tool("save_file", {"destination_path": "~/Downloads/flow.png"}),
        {"role": "assistant", "content": "Saved."},
    ]
    assert rf.requested_file_feedback(msgs, None) is None


def test_plan_mode_is_exempt():
    msgs = [
        {"role": "user", "content": "make me a pdf of the plan"},
        {"role": "assistant", "content": "Here is the plan."},
    ]
    assert rf.requested_file_feedback(msgs, None, plan_mode=True) is None
    assert rf.requested_file_feedback(msgs, None, plan_mode=False) is not None


def test_one_nudge_per_turn():
    msgs = [
        {"role": "user", "content": "make me a pdf"},
        {"role": "assistant", "content": "here you go"},
        {"role": "user", "content": f"[completion gate] {rf.REQUEST_MARKER} produce it"},
        {"role": "assistant", "content": "still no file"},
    ]
    assert rf.request_nudges_used(msgs) == 1
    assert rf.requested_file_feedback(msgs, None) is None


def test_the_ask_is_read_from_this_turn_not_an_older_one():
    msgs = [
        {"role": "user", "content": "make me a pdf"},
        {"role": "assistant", "content": "saved"},
        {"role": "user", "content": "thanks, what is in it?"},
        {"role": "assistant", "content": "three sections"},
    ]
    assert rf.requested_file_feedback(msgs, None) is None


# ── a message the user sends late in the turn ──────────────────────────────
# One that lands after the loop's last drain gets a user row of its own after
# the assistant turn (runner._remind). The turn's ask is still its prompt.


def _late(messages: list, text: str) -> list:
    _remind(messages, _midturn_text(midturn_inbox.Entry(midturn_inbox.USER, text)))
    return messages


def test_the_original_ask_is_still_checked_after_a_late_mid_turn_message():
    msgs = _late(
        [
            {"role": "user", "content": "generate a png of the flow and save it in downloads"},
            {"role": "assistant", "content": "Here is a description of the flow."},
        ],
        "also mention the retry step",
    )
    msgs.append({"role": "assistant", "content": "Added the retry step."})
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and "generate a png of the flow and save it in downloads" in fb


def test_a_file_saved_before_a_late_mid_turn_message_still_counts():
    msgs = _late(
        [
            {"role": "user", "content": "generate a png of the flow and save it in downloads"},
            _tool("save_file", {"destination_path": "~/Downloads/flow.png"}),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {"role": "assistant", "content": "Saved."},
        ],
        "thanks, keep those colours",
    )
    msgs.append({"role": "assistant", "content": "Will do."})
    assert rf.produced_a_file(msgs, None, on_disk=True) is True
    assert rf.requested_file_feedback(msgs, None) is None


def test_one_nudge_per_turn_holds_across_a_late_mid_turn_message():
    msgs = _late(
        [
            {"role": "user", "content": "make me a pdf of the plan"},
            {"role": "assistant", "content": "Here is the plan."},
            {"role": "user", "content": f"[completion gate] {rf.REQUEST_MARKER} produce it"},
            {"role": "assistant", "content": "I cannot write a pdf here."},
        ],
        "then export it as a csv instead",
    )
    msgs.append({"role": "assistant", "content": "Here are the rows."})
    assert rf.request_nudges_used(msgs) == 1
    assert rf.requested_file_feedback(msgs, None) is None


# ── a file asked for while the turn runs ───────────────────────────────────
# "also make a PNG chart" sent while the report is being written is a request
# of its own. The prompt's request is answered by anything the turn made; the
# late one only by what the turn did after the message reached it.

_PROMPT = "write the q3 report as a docx"
_LATER = "also make a png chart of the totals"


def _report_saved(report) -> list:
    """The prompt, and a save_file round whose result row is where a message
    sent during it lands (loop_hints.inject_reminder)."""
    return [
        {"role": "user", "content": _PROMPT},
        _tool("save_file", {"destination_path": str(report)}),
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": f"Saved to {report}"}
            ],
        },
    ]


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "Q3 Report.docx"
    path.write_bytes(b"docx")
    return path


def test_a_file_asked_for_mid_turn_is_owed_after_the_message(report):
    msgs = _late(_report_saved(report), _LATER)
    msgs.append({"role": "assistant", "content": "The totals are 3, 5 and 8."})
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and fb.startswith(rf.REQUEST_MARKER)
    assert _LATER in fb and _PROMPT not in fb


def test_restating_the_earlier_file_does_not_answer_the_later_request(report):
    # The reply that answers the late message usually recaps the turn.
    msgs = _late(_report_saved(report), _LATER)
    msgs.append(
        {"role": "assistant", "content": f"Saved the report to `{report}`. The totals are 3, 5, 8."}
    )
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and _LATER in fb


@pytest.mark.parametrize(
    "after",
    [
        # A file tool after the message.
        lambda tmp: [_tool("create_chart"), {"role": "assistant", "content": "Chart added."}],
        # A reply after the message naming a new file that exists.
        lambda tmp: [{"role": "assistant", "content": f"Saved the chart to `{tmp}/totals.png`."}],
        # A script after the message rewrote the report with the chart in it.
        lambda tmp: [
            _tool("run_python"),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {
                "role": "assistant",
                "content": f"Saved the report with the chart to `{tmp}/q3.docx`.",
            },
        ],
    ],
    ids=["file-tool", "new-file-named", "script-rewrote-it"],
)
def test_a_file_made_after_the_message_answers_it(after, tmp_path):
    (tmp_path / "totals.png").write_bytes(b"png")
    (tmp_path / "q3.docx").write_bytes(b"docx")
    msgs = _report_saved(tmp_path / "q3.docx")
    _late(msgs, _LATER)
    msgs += after(tmp_path)
    assert rf.requested_file_feedback(msgs, None) is None


def test_a_message_folded_onto_the_prompt_has_the_whole_turn(report):
    # It reached the turn before the first round: the save answers both.
    msgs = _late([{"role": "user", "content": _PROMPT}], _LATER)
    msgs += _report_saved(report)[1:]
    msgs.append({"role": "assistant", "content": f"Saved to `{report}`."})
    assert rf.file_requests(msgs) == [(_PROMPT, 0), (_LATER, 0)]
    assert rf.requested_file_feedback(msgs, None) is None


def test_a_late_message_after_the_reply_is_owed_by_what_follows_it(report):
    msgs = _report_saved(report) + [{"role": "assistant", "content": f"Saved to `{report}`."}]
    _late(msgs, _LATER)  # a row of its own after the assistant tail
    assert msgs[-1]["role"] == "user"
    msgs.append({"role": "assistant", "content": "The totals are 3, 5 and 8."})
    # The turn's rows 0 to 2 are the save, its result and the reply; the
    # message is row 3, so only row 4 on can answer it.
    assert rf.file_requests(msgs) == [(_PROMPT, 0), (_LATER, 4)]
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and _LATER in fb and _PROMPT not in fb


def test_one_nudge_names_every_request_still_owed():
    msgs = _late(
        [
            {"role": "user", "content": _PROMPT},
            {"role": "assistant", "content": "Drafting it."},
            {
                "role": "user",
                "content": "<system-reminder>Only 5 tool rounds remain.</system-reminder>",
            },
        ],
        _LATER,
    )
    msgs.append({"role": "assistant", "content": "The report says revenue grew."})
    fb = rf.requested_file_feedback(msgs, None)
    assert fb and _PROMPT in fb and _LATER in fb
    msgs += [
        {"role": "user", "content": f"[completion gate] {fb}"},
        {"role": "assistant", "content": "I did not make either file."},
    ]
    assert rf.requested_file_feedback(msgs, None) is None


def test_calling_the_file_off_mid_turn_asks_for_nothing(report):
    msgs = _late(_report_saved(report), "no need to save a chart file, just tell me the totals")
    msgs.append({"role": "assistant", "content": "The totals are 3, 5 and 8."})
    assert rf.requested_file_feedback(msgs, None) is None


# ── the gate phase ─────────────────────────────────────────────────────────


@pytest.fixture
def _gate_env(monkeypatch):
    from server import hooks
    from server.goals import gate

    async def no_stop(*a, **k):
        return SimpleNamespace(blocked=False, reason="")

    monkeypatch.setattr(hooks, "check_stop_hooks", no_stop)
    monkeypatch.setattr(gate, "_flag_on", lambda name, default=True: name == "requested_file_check")
    return gate


def test_gate_blocks_when_a_requested_file_was_never_produced(_gate_env):
    from server.goals import GateContext

    msgs = [
        {"role": "user", "content": "generate a png of the flow and save it in downloads"},
        {"role": "assistant", "content": "Here is a description of the flow."},
    ]
    decision = asyncio.run(
        _gate_env.run_completion_gate(GateContext(session_id="s", messages=msgs))
    )
    assert decision.block is True and decision.source == "requested_file"
    assert decision.feedback.startswith(rf.REQUEST_MARKER)


def test_gate_stays_out_of_plan_mode(_gate_env):
    from server.goals import GateContext

    msgs = [
        {"role": "user", "content": "generate a png of the flow and save it in downloads"},
        {"role": "assistant", "content": "Here is the plan."},
    ]
    decision = asyncio.run(
        _gate_env.run_completion_gate(GateContext(session_id="s", messages=msgs, plan_mode=True))
    )
    assert decision.block is False


# ── the engine ─────────────────────────────────────────────────────────────
# A bare TurnContext, a scripted adapter and a faked tool batch (the pattern
# from tests/test_gate_final_reply.py), with the mid-turn inbox the chat
# route reads: no HTTP route, no real Bedrock.


class _SaveThenAnswer:
    """Round 0 saves the report, and while it runs the user asks for a chart
    as well; every later round answers with the next scripted reply."""

    provider = "test"

    def __init__(self, sid: str, report, *replies: str):
        self.sid = sid
        self.report = report
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == 0:
            midturn_inbox.push(self.sid, _LATER)
            content = [
                {"type": "text", "text": "Saving the report."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "save_file",
                    "input": {"destination_path": str(self.report)},
                },
            ]
            yield TextDelta(text=content[0]["text"])
            yield RoundResult(stop_reason="tool_use", content=content, usage=Usage())
            return
        text = self.replies.pop(0)
        yield TextDelta(text=text)
        yield RoundResult(
            stop_reason="end_turn", content=[{"type": "text", "text": text}], usage=Usage()
        )


def test_the_engine_asks_for_a_file_requested_mid_turn(_gate_env, monkeypatch, report):
    import server.tool_executor as TE

    async def batch(tool_uses, **kw):
        return list(tool_uses)

    async def process(states, budget_fn, **kw):
        results = [
            {"type": "tool_result", "tool_use_id": t["id"], "content": f"Saved to {report}"}
            for t in states
        ]
        return (results, [], False, False)

    monkeypatch.setattr(TE, "execute_tool_batch", batch)
    monkeypatch.setattr(TE, "process_tool_results", process)

    sid = "mid-turn-file-request"
    midturn_inbox.clear(sid)
    recap = f"Saved the report to `{report}`. The totals are 3, 5 and 8."
    adapter = _SaveThenAnswer(sid, report, recap, "I have not made the chart: no data source.")
    ctx = TurnContext(
        cost_source="chat",
        session_id=sid,
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": _PROMPT}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=10, completion_gate=True),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        midturn_inbox=True,
    )

    async def go():
        return "".join([c async for c in run_turn(ctx)])

    out = asyncio.run(go())

    # The message landed on the save's result row, after the save.
    result_row = adapter.calls[1][-1]
    assert result_row["content"][0]["type"] == "tool_result"
    assert _LATER in result_row["content"][-1]["text"]
    # The recap round ended with the chart owed; the gate asked for it once.
    assert len(adapter.calls) == 3
    nudge = adapter.calls[2][-1]
    assert nudge["content"].startswith(f"[completion gate] {rf.REQUEST_MARKER}")
    assert _LATER in nudge["content"] and _PROMPT not in nudge["content"]
    assert out.count("stop_hook_block") == 1
    assert midturn_inbox.has_pending(sid) is False
