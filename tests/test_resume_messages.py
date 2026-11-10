"""How a continuation turn (approval decision / ask_user answer) rebuilds its
message list: server/chat/resume.resume_messages.

Two paths, and the second one used to be broken in a way that never surfaced:

  1. Paused state present (the normal case): the stashed messages come back and
     the placeholder tool_results are filled in BY tool_use_id, so every
     tool_use in the preceding assistant message is answered. Bedrock rejects
     the whole request otherwise.

  2. Paused state gone (the backend restarted while the card sat on screen): the
     assistant message carrying the tool_use went with it, so emitting a
     tool_result block produced an ORPHAN — sanitize_tool_pairs strips orphans,
     which left the model answering a turn containing neither the tool call nor
     its outcome. The old comment claimed Bedrock would "error loudly"; it could
     not. The outcome is now replayed as plain text on top of the rebuilt
     history, which every provider accepts.
"""

from server.chat.compaction import sanitize_tool_pairs
from server.chat.resume import resume_messages as _resume_messages


def _paused():
    """A pause stashed mid-turn: assistant asked for a tool, placeholder pending."""
    return {
        "messages": [
            {"role": "user", "content": "make me a branch"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "git_create_branch",
                        "input": {"name": "docs/x"},
                    }
                ],
            },
        ],
        "pending_tool_results": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "[Not executed] …"}
        ],
        "provider": "openai",
    }


ANSWER = {"tool_use_id": "tu_1", "content": "[User approved] git_create_branch: docs/x."}


def test_paused_state_fills_the_placeholder_by_tool_use_id():
    out = _resume_messages([], [ANSWER], _paused())

    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    blocks = out[-1]["content"]
    assert len(blocks) == 1
    assert blocks[0]["tool_use_id"] == "tu_1"
    assert blocks[0]["content"] == ANSWER["content"]
    # Well-formed: the pair survives the send-time sanitizer untouched.
    assert sanitize_tool_pairs(out) == out


def test_paused_state_appends_an_answer_with_no_placeholder():
    paused = _paused()
    paused["pending_tool_results"] = []

    out = _resume_messages([], [ANSWER], paused)

    blocks = out[-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["tu_1"]
    assert blocks[0]["content"] == ANSWER["content"]


def test_paused_state_is_not_mutated_in_place():
    """The stash is popped, but a retry/second caller must not see a half-built
    list — build a new one rather than appending onto the stashed messages."""
    paused = _paused()
    stashed = paused["messages"]

    _resume_messages([], [ANSWER], paused)

    assert len(stashed) == 2  # unchanged


def test_lost_pause_replays_the_outcome_as_text_on_top_of_history():
    history = [
        {"role": "user", "content": "make me a branch"},
        {"role": "assistant", "content": "I'll create it."},
    ]

    out = _resume_messages(history, [ANSWER], None)

    assert len(out) == 3
    assert out[:2] == history  # the conversation is still there
    last = out[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], str)
    assert "[User approved] git_create_branch: docs/x." in last["content"]
    assert "restarted" in last["content"]


def test_lost_pause_survives_the_send_time_sanitizer():
    """The whole point: no tool_result block means nothing to strip, so the
    outcome actually reaches the model instead of being silently removed."""
    history = [{"role": "user", "content": "make me a branch"}]

    out = _resume_messages(history, [ANSWER], None)
    sanitized = sanitize_tool_pairs(out)

    assert sanitized == out
    assert ANSWER["content"] in sanitized[-1]["content"]


def test_lost_pause_replays_every_answer_of_a_batch():
    answers = [
        {"tool_use_id": "tu_a", "content": "[User answered] region: eu-central-1"},
        {"tool_use_id": "tu_b", "content": "[User answered] tier: premium"},
    ]

    out = _resume_messages([], answers, None)

    assert "eu-central-1" in out[-1]["content"]
    assert "premium" in out[-1]["content"]


def test_old_orphan_shape_would_have_been_stripped():
    """Pins WHY this changed: the shape the fallback used to build loses the
    outcome entirely at send time, which is what made a post-restart approval
    answer from nothing."""
    orphan = [
        {"role": "user", "content": "make me a branch"},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": ANSWER["content"]}
            ],
        },
    ]

    sanitized = sanitize_tool_pairs(orphan)

    assert ANSWER["content"] not in str(sanitized[-1]["content"])


def test_a_question_left_unanswered_gets_a_plain_note_not_the_pause_marker():
    """Two questions paused the round; the continuation answered one. The other
    must not go back to the model as a marker that promises an answer."""
    from server.tool_executor import ASK_PAUSE_MARKER, ASK_UNANSWERED

    paused = _paused()
    paused["messages"][1]["content"] = [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "ask_user_question",
            "input": {"question": "One?"},
        },
        {
            "type": "tool_use",
            "id": "tu_2",
            "name": "ask_user_question",
            "input": {"question": "Two?"},
        },
    ]
    paused["pending_tool_results"] = [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": ASK_PAUSE_MARKER},
        {"type": "tool_result", "tool_use_id": "tu_2", "content": ASK_PAUSE_MARKER},
    ]

    out = _resume_messages([], [{"tool_use_id": "tu_1", "content": "capacity-planner"}], paused)

    blocks = {b["tool_use_id"]: b["content"] for b in out[-1]["content"]}
    assert blocks == {"tu_1": "capacity-planner", "tu_2": ASK_UNANSWERED}
    assert sanitize_tool_pairs(out) == out


# A new message sent while the cards waited (server/chat/resume.resume_with_message).


def test_a_new_message_closes_the_waiting_approval_and_joins_the_same_turn():
    """The user typed instead of answering the card: the paused call is closed
    as not run, and the message sits after its tool_result in one user turn."""
    from server.chat.resume import CARD_SKIPPED, resume_with_message

    history = [{"role": "user", "content": "client history, without the tool call"}]
    new_message = {"role": "user", "content": "Stop here. Tell me what two.txt contains."}

    out = resume_with_message([*history, new_message], ["tu_1"], _paused())

    assert out[:-1] == _paused()["messages"]
    tool_result, text = out[-1]["content"]
    assert tool_result == {"type": "tool_result", "tool_use_id": "tu_1", "content": CARD_SKIPPED}
    assert text == {"type": "text", "text": new_message["content"]}
    assert sanitize_tool_pairs(out) == out


def test_a_skipped_question_says_not_answered_and_keeps_the_message_files():
    from server.chat.resume import resume_with_message
    from server.tool_executor import ASK_PAUSE_MARKER, ASK_UNANSWERED

    paused = _paused()
    paused["pending_tool_results"] = [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": ASK_PAUSE_MARKER}
    ]
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    new_message = {"role": "user", "content": [image, {"type": "text", "text": "What is this?"}]}

    out = resume_with_message([new_message], ["tu_1"], paused)

    assert out[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": ASK_UNANSWERED},
        image,
        {"type": "text", "text": "What is this?"},
    ]


def test_a_skipped_card_after_a_restart_is_replayed_as_text_before_the_message():
    from server.chat.resume import CARD_SKIPPED, resume_with_message

    history = [{"role": "user", "content": "make me a branch"}]
    new_message = {"role": "user", "content": "Never mind, list the branches."}

    out = resume_with_message([*history, new_message], ["tu_1"], None)

    assert out[0] == history[0]
    replay, text = out[-1]["content"]
    assert CARD_SKIPPED in replay["text"]
    assert text["text"] == new_message["content"]
    assert sanitize_tool_pairs(out) == out
