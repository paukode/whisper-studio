"""A catalog key, its Bedrock id and the ladder it declares tell one story.

Model keys are matched by regex in several places (the effort ladder, the
ultracode capability, the family alias of an agent override), and each reads
the version out of the key or out of the Bedrock id. These checks hold for
every shipped entry, so a new model (Sonnet 5.5 was the first dotted Sonnet
key) resolves the same whichever of the two a caller reads.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from server.infrastructure.effort import (
    effort_levels_for,
    infer_effort_tier,
    openai_effort_tier_for,
)

_EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "config.example.json")
_CLAUDE_KEY = re.compile(r"^(opus|sonnet|fable|haiku)(\d+)(?:\.(\d+))?$")


def _catalog() -> dict:
    with open(_EXAMPLE) as f:
        return json.load(f)["chat_models"]


def _claude_id_version(family: str, model_id: str) -> tuple[int, int] | None:
    m = re.search(rf"claude-{family}-(\d+)(?:-(\d{{1,2}})(?!\d))?", model_id)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def test_a_versioned_claude_key_names_the_version_of_its_bedrock_id():
    checked = []
    for key, entry in _catalog().items():
        m = _CLAUDE_KEY.match(key)
        if not m:
            continue
        family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        assert _claude_id_version(family, entry["id"]) == (major, minor), (key, entry["id"])
        checked.append(key)
    assert checked


def test_a_declared_ladder_is_the_one_its_bedrock_id_infers():
    # An entry renamed by the user, or written as a bare id string, keeps the
    # ladder of the model it points at: inference from the id alone must reach
    # the tier the shipped entry declares.
    checked = []
    for key, entry in _catalog().items():
        declared = entry.get("effort_tier")
        if not declared or entry.get("is_local"):
            continue
        model_id = entry["id"]
        if model_id.startswith("openai."):
            inferred = openai_effort_tier_for(model_id)
        else:
            inferred = infer_effort_tier("", model_id)
        assert inferred == declared, (key, model_id, inferred)
        checked.append(key)
    assert checked


@pytest.mark.parametrize("version", [(4, 5), (4, 6), (5, 0), (5, 5), (5, 6), (6, 0)])
def test_the_app_offers_sonnet_the_xhigh_rung_from_5_5(version):
    # The app keeps Sonnet 5 and earlier on the standard ladder and offers
    # xhigh ("extra") from Sonnet 5.5 on. The key and the Bedrock id each carry
    # the version and must agree.
    major, minor = version
    model_id = f"global.anthropic.claude-sonnet-{major}" + (f"-{minor}" if minor else "")
    key = f"sonnet{major}.{minor}"
    expected = "full" if version >= (5, 5) else "standard"
    assert infer_effort_tier(key) == expected
    assert infer_effort_tier("my-sonnet", model_id) == expected
    levels = effort_levels_for({"id": model_id}, key)
    assert ("extra" in levels) is (expected == "full")
