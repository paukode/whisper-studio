"""Where GPT runs, and what the user sees when a model's region cannot serve it.

bedrock-mantle serves each GPT model in its own set of US regions (measured
per model; every EU region answers 404). Nothing reroutes a call: the 404 has
to name where the model does run and the setting that gets it there, and say
nothing it does not know for a model whose regions were never measured.
"""

from __future__ import annotations

import pytest

from server.chat.engine.openai import _friendly_error
from server.infrastructure.config import load_config
from server.openai_bedrock import runtime as oai

_NOT_FOUND = Exception("Error code: 404 - {'error': {'message': 'The model does not exist'}}")


def _gpt_keys() -> list[str]:
    keys = [k for k in load_config()["chat_models"] if oai.is_openai_model(k)]
    assert keys
    return keys


def _meta(key: str) -> dict:
    return oai._model_meta(key)


def _served(key: str) -> tuple[str, ...] | None:
    return oai.gpt_serving_regions(_meta(key)["id"])


def test_every_shipped_gpt_model_has_measured_regions_among_the_gpt_regions():
    for key in _gpt_keys():
        served = _served(key)
        assert served, f"{key} has no measured regions: probe it before shipping it"
        assert set(served) <= set(oai.GPT_REGIONS), key


@pytest.mark.parametrize("region", ["us-east-1", "us-east-2", "us-west-2"])
def test_each_probed_region_serves_some_shipped_gpt_models_and_not_others(region):
    # The 2026-09-29 probe (one call per model and region) found every US
    # region it tried serving part of the catalog, which is why the rule is
    # per model: us-east-2 has the GPT-5 models only, us-west-2 lacks GPT-6
    # Sol and Luna, GPT-5.6 Sol and GPT-5.5, and us-east-1 lacks GPT-6 Astra.
    served_here = [region in _served(key) for key in _gpt_keys()]
    assert region in oai.GPT_REGIONS
    assert any(served_here) and not all(served_here), region


def test_every_shipped_gpt_model_resolves_to_a_region_bedrock_serves_it_in():
    for key in _gpt_keys():
        region = oai.region_for(key)
        assert region in _served(key), (key, region)
        assert oai.region_fix(key, region) is None


def test_a_wire_prefix_does_not_hide_a_models_regions():
    for key in _gpt_keys():
        model_id = _meta(key)["id"]
        assert oai.gpt_serving_regions(f"bedrock-mantle.{model_id}") == _served(key), key


@pytest.mark.parametrize("region", [*oai.GPT_REGIONS, "eu-central-1", "eu-west-1"])
def test_a_404_outside_a_models_regions_names_them_and_the_fix(region):
    checked = 0
    for key in _gpt_keys():
        served = _served(key)
        if region in served:
            continue
        text = _friendly_error(_NOT_FOUND, key, region)
        assert _meta(key)["label"] in text and f"not served in {region}" in text, text
        assert f"serves it only in {oai._spoken(served)}" in text, text
        choices = oai._spoken(served, "or")
        if (_meta(key).get("openai_region") or "").strip():
            # A pinned model follows its pin: bedrock_region would not help.
            assert f"change openai_region on the {key} entry in chat_models to {choices}" in text
        else:
            assert f"set bedrock_region to {choices}, or pin openai_region" in text, text
        assert "GPT-5.x" not in text
        checked += 1
    assert checked


def test_a_404_in_a_served_region_does_not_ask_for_another_region():
    for key in _gpt_keys():
        region = _served(key)[0]
        text = _friendly_error(_NOT_FOUND, key, region)
        assert "a region Bedrock serves it in" in text and "model access" in text, text
        assert "To fix it" not in text


def test_a_model_with_no_measured_regions_is_told_nothing_it_cannot_know(monkeypatch):
    real = oai._model_meta

    def meta(key):
        if key == "gpt-next":
            return {"id": "openai.gpt-next", "label": "GPT Next", "provider": "openai_bedrock"}
        return real(key)

    monkeypatch.setattr(oai, "_model_meta", meta)
    assert oai.region_fix("gpt-next", "eu-central-1") is None
    assert oai.region_problems(["gpt-next"]) == []
    assert oai.unmeasured_models(["gpt-next"]) == ["GPT Next"]
    text = _friendly_error(_NOT_FOUND, "gpt-next", "eu-central-1")
    assert "is not served in" not in text and "serves it only in" not in text
    assert oai.gpt_regions_note() in text
    assert "bedrock_region" in text and "openai_region on the gpt-next entry" in text


def test_astra_pinned_to_an_east_region_is_told_to_move_its_pin(monkeypatch):
    real = oai._model_meta

    def meta(key):
        entry = dict(real(key))
        if key == "gpt6-astra":
            entry["openai_region"] = "us-east-1"
        return entry

    monkeypatch.setattr(oai, "_model_meta", meta)
    assert oai.region_for("gpt6-astra") == "us-east-1"
    choices = oai._spoken(_served("gpt6-astra"), "or")
    assert (
        oai.region_fix("gpt6-astra", "us-east-1")
        == f"change openai_region on the gpt6-astra entry in chat_models to {choices}"
    )
