"""server.goals.requested_files: the user asked for a file and the turn made none.

The mirror of the claimed-deliverable check. Real session: "i want the visual
and ci file i shared at the start to update and save a new version in
downloads" was answered with a revised description in chat, no file written,
and the turn ended. The user learned it only by asking afterwards.
"""

import asyncio
from types import SimpleNamespace

import pytest

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
    assert rf.requested_clause(prompt) is not None


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
    assert rf.requested_clause(prompt) is None


def test_the_verb_and_the_noun_have_to_be_in_the_same_breath():
    # Reading one file then answering in chat owes the user nothing on disk.
    assert rf.requested_clause("read the pdf. then tell me what changed") is None
    assert rf.requested_clause("read the pdf, then write me a docx") is not None


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
