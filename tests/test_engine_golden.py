"""Golden-frame acceptance tests for the chat turn loop.

These pin the exact SSE frame sequences the Anthropic chat loop emits for the
core scenarios. They are the parity contract for the P2+ turn-engine cutover:
the new engine must reproduce every fixture frame-for-frame. See
tests/golden_harness.py for the harness and the GOLDEN_RECORD regen flow.
"""

from tests.golden_harness import (
    FakeBedrockClient,
    assert_golden,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    thinking_block,
    tool_use_block,
)


def test_golden_plain_text_turn(monkeypatch):
    client = FakeBedrockClient(
        [
            [msg_start(), *text_block("Hello there."), *msg_end()],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "say hello"})
    assert_golden("plain_text", lines)


def test_golden_thinking_then_text(monkeypatch):
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *thinking_block("pondering...", index=0),
                *text_block("Answer after thought.", index=1),
                *msg_end(),
            ],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "think first"})
    assert_golden("thinking_then_text", lines)


def test_golden_tool_round_then_answer(monkeypatch):
    # Round 1: the model calls analyze_document (deterministic offline result:
    # "No documents attached."). Round 2: it answers from the tool result.
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "analyze_document", {"filename": "x.md", "question": "?"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Nothing attached."), *msg_end()],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "read my file"})
    assert_golden("tool_round_then_answer", lines)
    # The follow-up request must carry the tool_use/tool_result pair.
    round2_msgs = client.requests[1]["messages"]
    assert round2_msgs[-2]["role"] == "assistant"
    assert round2_msgs[-1]["role"] == "user"
    assert round2_msgs[-1]["content"][0]["type"] == "tool_result"


def test_golden_ask_user_pause_and_resume(monkeypatch):
    import server.chat.routes as routes_mod

    routes_mod._paused_sessions.clear()
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block(
                    "tu_q",
                    "ask_user_question",
                    {"question": "Which env?", "options": ["dev", "prod"]},
                ),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Deploying to prod."), *msg_end()],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "deploy"})
    assert_golden("ask_user_pause", lines)
    assert "golden-session" in routes_mod._paused_sessions

    resume_lines = run_chat_turn(
        monkeypatch,
        client,
        {
            "question": "",
            "approved_tool_result": {"tool_use_id": "tu_q", "content": "prod"},
        },
    )
    assert_golden("ask_user_resume", resume_lines)
    assert "golden-session" not in routes_mod._paused_sessions


def test_golden_mid_stream_error(monkeypatch):
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *text_block("partial"),
                {"internalServerException": {"message": "boom"}},
            ],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "explode"})
    assert_golden("mid_stream_error", lines)
