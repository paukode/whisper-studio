"""A question card's choices: none (a typed answer only) or two or more, as
many as the user asked for, and never an "Other" choice. The card always has
a text field for an answer of the user's own, so an "Other" choice only took
the place of a real one: asked for three colors, the user saw two. A call
that breaks the rule is refused and goes back to the model, which asks again.
"""

import asyncio

import pytest

from server.ask_user import ASK_USER_TOOL, question_options_refusal
from server.tool_executor import tool_outcome
from server.tool_router import SIDE_EFFECT_PAUSE, route_tool


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["Blue", "Green"],
        ["Blue", "Green", "Red", "Yellow", "Purple", "Orange"],
        ["Other Inc.", "Acme"],
        ["Otherton, Jane", "Smith, Ann"],
    ],
)
def test_no_choices_or_two_and_more_real_ones_pass(options):
    assert question_options_refusal(options) is None


def test_one_choice_is_refused():
    assert question_options_refusal(["Blue"]).startswith("[Refused] One choice is no choice.")


@pytest.mark.parametrize(
    "other", ["Other", "other", "Other (please specify)", "Other: type it", "Other…"]
)
def test_an_other_choice_is_refused_by_name(other):
    refusal = question_options_refusal(["Blue", "Green", other])

    assert refusal.startswith(f"[Refused] Remove the choice {other!r}")
    assert "text field" in refusal


def test_the_tool_tells_the_model_the_rule_and_sets_no_upper_limit():
    options = ASK_USER_TOOL["input_schema"]["properties"]["options"]

    assert "maxItems" not in options
    assert "'Other'" in ASK_USER_TOOL["description"]
    assert "two or more" in ASK_USER_TOOL["description"]


def _ask(options):
    return asyncio.run(
        route_tool(
            "ask_user_question",
            {"question": "Which color do you like best?", "options": options},
            loop=None,
            executor=None,
            transcript="",
            attachments=None,
            session_id="sess-choices",
            model_id="m",
            tool_use_id="tu1",
        )
    )


def test_a_refused_call_shows_no_card_and_goes_back_to_the_model_as_an_error():
    output, side_effects = _ask(["Blue", "Green", "Other (please specify)"])

    assert side_effects == []
    assert tool_outcome("ask_user_question", output).is_error


def test_a_good_call_shows_the_card_and_pauses():
    output, side_effects = _ask(["Blue", "Green", "Red"])

    assert side_effects[0]["user_question"]["options"] == ["Blue", "Green", "Red"]
    assert {SIDE_EFFECT_PAUSE: True} in side_effects
    assert not tool_outcome("ask_user_question", output).is_error
