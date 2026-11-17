"""A chat turn that starts to build with no question gets one reminder."""

import asyncio

import pytest

from server.chat import ask_first
from server.chat.engine.events import RoundResult, Usage
from server.chat.engine.policy import TurnPolicy
from server.chat.engine.runner import TurnContext, run_turn
from server.prompts.base import BASE


def call(name, call_id="c1"):
    return {"type": "tool_use", "id": call_id, "name": name, "input": {}}


def result(call_id="c1"):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id}]}


def turn(*rounds):
    """A prompt, then one assistant message and one result row a round."""
    messages = [{"role": "user", "content": "design a storage box"}]
    for i, names in enumerate(rounds):
        calls = [call(name, f"c{i}-{j}") for j, name in enumerate(names)]
        messages.append({"role": "assistant", "content": calls})
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": c["id"]} for c in calls],
            }
        )
    return messages, [c for c in messages[-2]["content"]]


def test_the_first_build_call_with_no_question_gets_the_reminder():
    messages, uses = turn(["ws_read_file"], ["ws_write_file"])
    assert ask_first.reminder_for(messages, uses) == ask_first.REMINDER


def test_reading_and_running_are_not_a_build():
    messages, uses = turn(["ws_read_file", "ws_run_command"])
    assert ask_first.reminder_for(messages, uses) is None


def test_a_change_to_a_file_that_exists_is_not_a_build():
    messages, uses = turn(["ws_edit_file"])
    assert ask_first.reminder_for(messages, uses) is None


def test_a_question_earlier_in_the_turn_settles_it():
    messages, uses = turn(["ask_user_question"], ["create_docx"])
    assert ask_first.reminder_for(messages, uses) is None


def test_a_question_in_the_same_round_settles_it():
    messages, uses = turn(["save_file", "ask_user_question"])
    assert ask_first.reminder_for(messages, uses) is None


def test_the_reminder_comes_once_a_turn():
    messages, uses = turn(["ws_write_file"], ["ws_write_file"])
    messages[2]["content"].append({"type": "text", "text": ask_first.REMINDER})
    assert ask_first.reminder_for(messages, uses) is None


def test_a_build_round_that_paused_for_a_card_is_reminded_in_the_next_round():
    # The round of the first build call stopped for its approval card, so it
    # carried no reminder: the next round of the turn does.
    messages, uses = turn(["ws_write_file"], ["ws_read_file"])
    assert ask_first.reminder_for(messages, uses) == ask_first.REMINDER


def test_a_turn_whose_start_became_a_summary_gets_no_reminder():
    from server.chat.compaction import SUMMARY_HEADER

    messages, uses = turn(["ws_write_file"])
    messages[0] = {"role": "user", "content": f"{SUMMARY_HEADER}\n1. Context: a box"}
    assert ask_first.reminder_for(messages, uses) is None


def test_a_new_prompt_starts_a_new_turn():
    earlier, _ = turn(["ws_write_file"])
    later, uses = turn(["create_artifact"])
    messages = [*earlier, {"role": "assistant", "content": "done"}, *later]
    assert ask_first.reminder_for(messages, uses) == ask_first.REMINDER


def test_the_reminder_is_an_engine_row_not_the_users_words():
    from server.goals.deliverables import _ENGINE_PREFIXES

    assert ask_first.REMINDER.startswith(_ENGINE_PREFIXES)
    assert ask_first.ASK_TOOL in ask_first.REMINDER


def test_every_build_tool_is_a_tool_the_app_has():
    from server.chat.tool_pool import assemble_full_catalog

    names = {t.get("name") for t in assemble_full_catalog()}
    assert ask_first.BUILD_TOOLS <= names


class _Builder:
    """Round 0 writes a file, round 1 answers."""

    provider = "test"

    def __init__(self):
        self.calls = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append([dict(m) for m in messages])
        if round_num == 0:
            yield RoundResult(
                stop_reason="tool_use", content=[call("ws_write_file")], usage=Usage()
            )
        else:
            yield RoundResult(
                stop_reason="end_turn", content=[{"type": "text", "text": "ok"}], usage=Usage()
            )


def _sent_to_round_two(monkeypatch, **kw) -> str:
    async def batch(tool_uses, **_):
        return []

    async def gate(states, **_):
        return (
            [{"type": "tool_result", "tool_use_id": "c1", "content": "written"}],
            [],
            False,
            False,
        )

    monkeypatch.setattr("server.tool_executor.execute_tool_batch", batch)
    monkeypatch.setattr("server.tool_executor.process_tool_results", gate)
    monkeypatch.setattr("server.chat.engine.activity_labels.start_label_task", lambda *a, **k: None)
    adapter = _Builder()
    ctx = TurnContext(
        cost_source="chat",
        session_id="ask-first-" + "-".join(sorted(kw)),
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "design a storage box"}],
        adapter=adapter,
        policy=TurnPolicy(max_rounds=4, completion_gate=False),
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
        **kw,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    asyncio.run(go())
    assert len(adapter.calls) == 2
    tail = adapter.calls[1][-1]
    assert tail["role"] == "user"
    return "\n".join(str(b.get("text", "")) for b in tail["content"] if isinstance(b, dict))


def test_the_chat_turn_sends_the_reminder_with_the_results(monkeypatch):
    assert ask_first.REMINDER in _sent_to_round_two(monkeypatch, midturn_inbox=True)


@pytest.mark.parametrize(
    "kw",
    [
        {},  # an agent, a scheduled run, a wake turn
        {"midturn_inbox": True, "unattended": True},
        {"midturn_inbox": True, "plan_mode": True},
    ],
)
def test_other_turns_get_no_reminder(monkeypatch, kw):
    assert ask_first.REMINDER not in _sent_to_round_two(monkeypatch, **kw)


def test_the_rule_comes_before_the_keep_working_rules():
    from server.prompts.base import ACT_BEFORE_ASKING, ASK_BEFORE_LONG_WORK, KEEP_WORKING

    assert (
        BASE.index(ASK_BEFORE_LONG_WORK) < BASE.index(KEEP_WORKING) < BASE.index(ACT_BEFORE_ASKING)
    )
    assert "you may ask" not in ASK_BEFORE_LONG_WORK


# The reminder follows how the user treats question cards.


@pytest.fixture
def habit(monkeypatch, tmp_path):
    from server.chat import ask_habit

    monkeypatch.setattr(ask_habit, "_path", lambda: str(tmp_path / "ask_habit.json"))
    return ask_habit


def _asked(call_id="q1", name="ask_user_question"):
    return [{"role": "assistant", "content": [call(name, call_id)]}]


def test_skipped_question_cards_in_a_row_make_the_reminder_quiet(habit):
    for _ in range(habit.SKIPS_TO_QUIET - 1):
        habit.note_cards(_asked(), ["q1"], answered=False)
    assert not habit.quiet()
    habit.note_cards(_asked(), ["q1"], answered=False)
    assert habit.quiet()


def test_one_answered_card_turns_the_reminder_back_on(habit):
    for _ in range(habit.SKIPS_TO_QUIET):
        habit.note_cards(_asked(), ["q1"], answered=False)
    habit.note_cards(_asked(), ["q1"], answered=True)
    assert not habit.quiet() and habit.skips_in_a_row() == 0


def test_a_stepper_of_questions_is_one_card(habit):
    history = [
        {
            "role": "assistant",
            "content": [call("ask_user_question", "a"), call("ask_user_question", "b")],
        }
    ]
    habit.note_cards(history, ["a", "b"], answered=False)
    assert habit.skips_in_a_row() == 1


def test_approval_cards_do_not_count(habit):
    for _ in range(habit.SKIPS_TO_QUIET):
        habit.note_cards(_asked("w1", "ws_write_file"), ["w1"], answered=False)
    assert habit.skips_in_a_row() == 0


def test_a_broken_habit_file_reads_as_no_skips(habit, tmp_path):
    (tmp_path / "ask_habit.json").write_text("not json")
    assert habit.skips_in_a_row() == 0 and not habit.quiet()


def test_a_quiet_habit_stops_the_reminder_in_the_chat_turn(monkeypatch, habit):
    for _ in range(habit.SKIPS_TO_QUIET):
        habit.note_cards(_asked(), ["q1"], answered=False)
    assert ask_first.REMINDER not in _sent_to_round_two(monkeypatch, midturn_inbox=True)


def test_the_memory_review_looks_at_how_the_user_wants_to_be_asked():
    from server.memory import prompts

    assert "wants to be asked" in prompts.EXTRACTION_SYSTEM_PROMPT


def _answer(call_id, text):
    return {"tool_use_id": call_id, "content": text}


def test_a_card_skipped_with_its_skip_buttons_counts_as_skipped(habit):
    from server.ask_user import SKIPPED_ANSWER_PREFIX

    history = [
        {
            "role": "assistant",
            "content": [call("ask_user_question", "a"), call("ask_user_question", "b")],
        }
    ]
    skip = SKIPPED_ANSWER_PREFIX + " Decide it yourself."
    habit.note_answers(history, [_answer("a", skip), _answer("b", skip)])
    assert habit.skips_in_a_row() == 1
    # One real answer on the card makes it an answered card.
    habit.note_answers(history, [_answer("a", skip), _answer("b", "blue")])
    assert habit.skips_in_a_row() == 0


def test_the_card_sends_the_skip_answer_the_server_reads():
    import pathlib
    import re

    from server.ask_user import SKIPPED_ANSWER_PREFIX

    card = pathlib.Path(__file__).parent.parent / "src/components/chat/UserQuestionCard.tsx"
    sent = re.search(r"export const SKIPPED_ANSWER =\s*'([^']+)'", card.read_text())
    assert sent and sent.group(1).startswith(SKIPPED_ANSWER_PREFIX)
