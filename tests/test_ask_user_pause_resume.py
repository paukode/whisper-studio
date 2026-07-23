"""Pause-and-resume tests for the ask_user_question tool.

The bug fixed in this commit: chat.py used to NOT save _paused_sessions when
a tool batch contained ask_user_question, so the user's answer arrived as a
fresh user message and Bedrock saw a tool_use without a matching tool_result.
The model then "ran to provide an answer" by hallucinating one.

These tests pin the contract:
  1. process_tool_results emits a placeholder tool_result for every
     ask_user_question tool_use_id, so the messages array is well-formed.
  2. The placeholder is the new ASK_USER_PAUSE marker — a future
     continuation turn can find and replace it by tool_use_id.
  3. has_user_question is True so the chat handler stashes _paused_sessions.
"""

import asyncio

from server.tool_executor import process_tool_results
from server.tool_router import SIDE_EFFECT_PAUSE


class _State:
    """Mimics the StreamingToolExecutor state shape used by process_tool_results."""

    def __init__(self, tool_id, tool_name, output, side_effects=None):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = output
        self.side_effects = side_effects or []
        self.status = "pending"


def _budget_passthrough(_name, output):
    return output


def _run(states):
    return asyncio.run(
        process_tool_results(
            states,
            budget_fn=_budget_passthrough,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )


def test_single_ask_user_question_pauses_and_emits_placeholder():
    state = _State(
        tool_id="tu_1",
        tool_name="ask_user_question",
        output="[PAUSE] Waiting for user to answer the question.",
        side_effects=[
            {
                "user_question": {
                    "question": "What's your level?",
                    "options": ["Beginner", "Intermediate"],
                    "tool_use_id": "tu_1",
                }
            },
            {SIDE_EFFECT_PAUSE: True},
        ],
    )
    tool_results, sse_events, has_pending_approval, has_user_question = _run([state])

    assert has_user_question is True
    assert has_pending_approval is False
    assert any('"user_question":' in e for e in sse_events)
    # Bedrock requires every tool_use to have a matching tool_result block.
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert "[ASK_USER_PAUSE]" in tool_results[0]["content"]
    assert "[PAUSE]" not in tool_results[0]["content"]


def test_multiple_ask_user_questions_get_independent_placeholders():
    """The model can fire several ask_user_question calls in one round
    (parallel tool_use). Each one must get its own placeholder, keyed by
    tool_use_id, so the continuation can fill them all in independently."""
    states = [
        _State(
            tool_id="tu_a",
            tool_name="ask_user_question",
            output="[PAUSE] q1",
            side_effects=[
                {
                    "user_question": {
                        "question": "Q1?",
                        "options": ["A", "B"],
                        "tool_use_id": "tu_a",
                    }
                },
                {SIDE_EFFECT_PAUSE: True},
            ],
        ),
        _State(
            tool_id="tu_b",
            tool_name="ask_user_question",
            output="[PAUSE] q2",
            side_effects=[
                {
                    "user_question": {
                        "question": "Q2?",
                        "options": ["X", "Y"],
                        "tool_use_id": "tu_b",
                    }
                },
                {SIDE_EFFECT_PAUSE: True},
            ],
        ),
    ]
    tool_results, sse_events, has_pending_approval, has_user_question = _run(states)

    assert has_user_question is True
    assert has_pending_approval is False
    # Two user_question SSE events emitted, one per tool_use. We match the
    # JSON key precisely so the skill_result preview events (which include
    # the skill name "ask_user_question" as a value) don't get counted.
    user_q_events = [e for e in sse_events if '"user_question":' in e]
    assert len(user_q_events) == 2
    # Two tool_results, one per tool_use_id, both marked as placeholders.
    by_id = {r["tool_use_id"]: r for r in tool_results}
    assert set(by_id.keys()) == {"tu_a", "tu_b"}
    for r in tool_results:
        assert "[ASK_USER_PAUSE]" in r["content"]
