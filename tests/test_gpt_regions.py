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


def test_us_east_1_serves_every_shipped_gpt_model():
    # The app's rule: every model runs in us-east-1 (the shipped
    # bedrock_region), and a model is pinned elsewhere only when us-east-1
    # does not serve it.
    assert all("us-east-1" in _served(key) for key in _gpt_keys())


@pytest.mark.parametrize("region", ["us-east-2", "us-west-2"])
def test_the_other_us_regions_serve_none_of_the_shipped_gpt_models(region):
    # The probes (one call per model and region) found each shipped model in
    # us-east-1 only.
    assert not any(region in _served(key) for key in _gpt_keys())


def test_the_regions_note_names_every_gpt_region_and_only_those():
    note = oai.gpt_regions_note()
    assert oai._spoken(oai.GPT_REGIONS) in note
    assert ("not every model in each" in note) == (len(oai.GPT_REGIONS) > 1)


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
    assert checked or all(region in _served(key) for key in _gpt_keys())


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


def test_a_pin_outside_a_models_regions_is_told_to_move(monkeypatch):
    real = oai._model_meta

    def meta(key):
        entry = dict(real(key))
        if key == "gpt6.2-sol":
            entry["openai_region"] = "us-west-2"
        return entry

    monkeypatch.setattr(oai, "_model_meta", meta)
    assert oai.region_for("gpt6.2-sol") == "us-west-2"
    choices = oai._spoken(_served("gpt6.2-sol"), "or")
    assert (
        oai.region_fix("gpt6.2-sol", "us-west-2")
        == f"change openai_region on the gpt6.2-sol entry in chat_models to {choices}"
    )


def test_every_shipped_gpt_model_follows_the_settings_region():
    for key in _gpt_keys():
        assert not (_meta(key).get("openai_region") or ""), key
        assert oai.region_for(key) == load_config()["bedrock_region"], key


def test_a_saved_region_pin_stands_when_read_back():
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {"gpt6.2-sol": {"id": "openai.gpt-6.2-sol", "openai_region": "us-west-2"}}
    )
    assert meta["gpt6.2-sol"]["openai_region"] == "us-west-2"


@pytest.mark.parametrize("key", ["gpt6.2-astra", "gpt6.2-sol"])
def test_the_gpt_6_2_models_run_in_us_east_1_on_the_gpt_6_ladder(key):
    model_id = _meta(key)["id"]
    assert model_id == f"openai.gpt-{key.removeprefix('gpt')}"
    assert oai.gpt_serving_regions(model_id) == ("us-east-1",)
    assert _meta(key)["effort_tier"] == "openai6"
    assert oai.reasoning_effort_for(key, "max") == "max"
    assert oai.reasoning_effort_for(key, "none") == "low"
