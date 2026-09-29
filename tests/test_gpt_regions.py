"""Where GPT runs, and what the user sees when a model's region cannot serve it.

bedrock-mantle serves the GPT models only in some US regions (every EU region
answers 404), and GPT-6 Astra only in us-west-2. Nothing reroutes a call: the
404 has to name where the model does run and the setting that gets it there.
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


def test_every_shipped_gpt_model_resolves_to_a_region_bedrock_serves_it_in():
    for key in _gpt_keys():
        region = oai.region_for(key)
        assert region in oai.gpt_serving_regions(_meta(key)["id"]), (key, region)
        assert oai.region_fix(key, region) is None


def test_a_model_is_never_served_outside_the_gpt_regions():
    for key in _gpt_keys():
        served = oai.gpt_serving_regions(_meta(key)["id"])
        assert served and set(served) <= set(oai.GPT_REGIONS), key


@pytest.mark.parametrize("region", ["eu-central-1", "eu-west-1", "eu-north-1"])
def test_a_404_outside_the_served_regions_names_them_and_the_fix(region):
    for key in _gpt_keys():
        served = oai.gpt_serving_regions(_meta(key)["id"])
        if region in served:
            continue
        text = _friendly_error(_NOT_FOUND, key, region)
        assert _meta(key)["label"] in text and region in text, (key, text)
        # Every region that serves GPT, and the ones this model runs in.
        for r in (*oai.GPT_REGIONS, *served):
            assert r in text, (key, r, text)
        assert "GPT-6 Astra only in us-west-2" in text
        if (_meta(key).get("openai_region") or "").strip():
            # A pinned model follows its pin: bedrock_region would not help.
            assert f"change openai_region on the {key} entry" in text, text
        else:
            assert "set bedrock_region to" in text and "openai_region" in text, text
        assert "GPT-5.x" not in text


def test_a_404_in_a_served_region_does_not_ask_for_another_region():
    for key in _gpt_keys():
        region = oai.gpt_serving_regions(_meta(key)["id"])[0]
        text = _friendly_error(_NOT_FOUND, key, region)
        assert "was not found in" in text and "model access" in text, text
        assert "To fix it" not in text


def test_astra_pinned_to_an_east_region_is_told_to_move_its_pin(monkeypatch):
    real = oai._model_meta

    def meta(key):
        entry = dict(real(key))
        if key == "gpt6-astra":
            entry["openai_region"] = "us-east-1"
        return entry

    monkeypatch.setattr(oai, "_model_meta", meta)
    assert oai.region_for("gpt6-astra") == "us-east-1"
    fix = oai.region_fix("gpt6-astra", "us-east-1")
    assert fix == "change openai_region on the gpt6-astra entry in chat_models to us-west-2"
