"""The recall selector's reply parser: Haiku is told to answer with JSON only
and regularly wraps it in a code fence anyway. One production log had every
memory recall fail on that fence, so no turn ever saw its memories."""

import json

from server.memory.recall import _bare_json


def test_plain_json_passes_through():
    text = '{"selected": ["global/a.md"]}'
    assert json.loads(_bare_json(text)) == {"selected": ["global/a.md"]}


def test_json_fence_is_stripped():
    text = '```json\n{\n  "selected": [\n    "global/feedback_response_style.md"\n  ]\n}\n```'
    assert json.loads(_bare_json(text)) == {"selected": ["global/feedback_response_style.md"]}


def test_bare_fence_and_surrounding_prose_are_stripped():
    text = 'Here you go:\n```\n{"selected": []}\n```\nLet me know.'
    assert json.loads(_bare_json(text)) == {"selected": []}


def test_reply_without_an_object_is_returned_as_is():
    assert _bare_json("I could not decide.") == "I could not decide."
