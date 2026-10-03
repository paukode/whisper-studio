"""/doctor's AWS rows report what boto3 actually resolves, and nothing in
Local mode.

The credential row used to be a file and env-var heuristic: an SSO or
credential_process profile with no ~/.aws/credentials was an "error" while
Bedrock worked, and an empty credentials file was "ok". It now asks botocore's
real provider chain. The AWS home is pinned to a temp dir and the Bedrock ping
is stubbed, so nothing here opens a socket.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import doctor


@pytest.fixture
def aws_home(tmp_path, monkeypatch):
    for var in list(os.environ):
        if var.startswith("AWS_"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", str(tmp_path / "boto.cfg"))
    return tmp_path


def _mode(monkeypatch, mode: str) -> None:
    # Patched where /doctor resolves it (a lazy import inside the handler).
    monkeypatch.setattr("server.infrastructure.model_mode.current_mode", lambda *a, **k: mode)


@pytest.fixture
def stub_bedrock(monkeypatch):
    import boto3

    class _Runtime:
        def invoke_model(self, **_kw):
            return {}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Runtime())


def _row(name: str) -> dict:
    app = FastAPI()
    app.include_router(doctor.router)
    data = TestClient(app).get("/api/doctor").json()
    return next(r for r in data["checks"] if r["check"] == name)


def test_credential_process_profile_without_credentials_file_is_ok(
    aws_home, stub_bedrock, monkeypatch
):
    _mode(monkeypatch, "cloud")
    script = aws_home / "creds.py"
    script.write_text(
        "import json; print(json.dumps({'Version': 1, 'AccessKeyId': 'AKIDEXAMPLE', "
        "'SecretAccessKey': 'example-secret'}))"
    )
    (aws_home / "config").write_text(f"[default]\ncredential_process = {sys.executable} {script}\n")
    assert not (aws_home / "credentials").exists()

    row = _row("AWS credentials")
    assert row["status"] == "ok"
    assert "custom-process" in row["detail"]


def test_empty_credentials_file_is_an_error(aws_home, stub_bedrock, monkeypatch):
    _mode(monkeypatch, "cloud")
    (aws_home / "credentials").write_text("")  # present, but holds no keys

    row = _row("AWS credentials")
    assert row["status"] == "error"
    assert "aws configure" in row["detail"]


def test_local_mode_skips_every_aws_check(aws_home, monkeypatch):
    import boto3

    _mode(monkeypatch, "local")

    def _no_aws(*_a, **_k):
        raise AssertionError("AWS touched in Local mode")

    monkeypatch.setattr(boto3, "client", _no_aws)
    monkeypatch.setattr("server.infrastructure.aws_clients.resolve_credentials", _no_aws)
    for name in ("AWS credentials", "Bedrock connectivity", "GPT regions"):
        row = _row(name)
        assert row["status"] == "ok"
        assert "Local mode" in row["detail"]


# ── The Config row names the model a turn would run on ──────────────────────


@pytest.fixture
def mode_layer(monkeypatch):
    """The user layer and the on-device registry, patched where production
    reads them, so the real load_config and model catalog run on top."""
    import json

    from server.infrastructure import config as cfg

    layer: dict = {}
    monkeypatch.setattr(cfg, "_load_user_config", lambda: json.loads(json.dumps(layer)))
    downloaded: dict = {}
    monkeypatch.setattr("server.local.registry.local_models", lambda: dict(downloaded))
    cfg._invalidate_cache()
    yield layer, downloaded
    cfg._invalidate_cache()


def _models_default() -> str:
    import asyncio

    from server.chat import routes

    return asyncio.run(routes.models_endpoint())["default"]


def test_a_fresh_local_install_says_no_chat_model_is_installed(aws_home, mode_layer):
    layer, _downloaded = mode_layer
    layer["model_mode"] = "local"
    row = _row("Config")
    assert _models_default() == ""
    assert row["status"] == "warn"
    assert "Settings > Models > Discover" in row["detail"]
    assert "region=" not in row["detail"]


@pytest.mark.parametrize("mode", ["local", "hybrid", "cloud"])
def test_the_config_row_names_the_model_the_picker_defaults_to(aws_home, mode_layer, mode):
    from server.local.registry import RECOMMENDED_LOCAL_MODELS

    layer, downloaded = mode_layer
    layer["model_mode"] = mode
    key = next(iter(RECOMMENDED_LOCAL_MODELS))
    downloaded[key] = dict(RECOMMENDED_LOCAL_MODELS[key])
    default = _models_default()
    assert default
    assert f"model={default}," in _row("Config")["detail"]


# ── GPT regions: every offered GPT model runs where Bedrock serves it ───────


def _gpt_models() -> dict:
    """{key: meta} of the GPT models the picker offers, as /doctor sees them."""
    from server.chat.infra import mode_chat_catalog
    from server.infrastructure.config import load_config

    visible, meta, _mode, _default = mode_chat_catalog(load_config())
    return {k: meta[k] for k in visible if meta[k].get("provider") == "openai_bedrock"}


def _problem_sentences(detail: str) -> list[str]:
    """The warning's sentences about models, without the closing note."""
    from server.openai_bedrock.runtime import gpt_regions_note

    problems = detail.replace(gpt_regions_note(), "").strip()
    return [s for s in problems.split(". ") if s]


def _named(detail: str) -> set[str]:
    """The GPT models a warning names."""
    sentences = _problem_sentences(detail)
    return {k for k, m in _gpt_models().items() if any(m["label"] in s for s in sentences)}


def test_the_shipped_gpt_models_pass_the_region_check(aws_home, mode_layer):
    assert _gpt_models()
    assert doctor._gpt_region_check()["status"] == "ok"


@pytest.mark.parametrize("region", ["eu-central-1", "us-west-2", "us-east-2"])
def test_a_region_warns_for_exactly_the_gpt_models_it_leaves_unserved(aws_home, mode_layer, region):
    # us-west-2 and us-east-2 serve some GPT models and not others (measured
    # per model); an EU region serves none of them.
    from server.openai_bedrock.runtime import (
        _spoken,
        gpt_regions_note,
        gpt_serving_regions,
        region_for,
    )

    layer, _downloaded = mode_layer
    layer["bedrock_region"] = region
    models = _gpt_models()
    served = {k: gpt_serving_regions(m["id"]) for k, m in models.items()}
    unserved = {k for k in models if region_for(k) not in served[k]}
    assert unserved

    row = doctor._gpt_region_check()
    assert row["status"] == "warn"
    assert _named(row["detail"]) == unserved
    assert row["detail"].endswith(gpt_regions_note())
    for key in unserved:
        (sentence,) = [s for s in _problem_sentences(row["detail"]) if models[key]["label"] in s]
        assert f"to {region}:" in sentence
        # Each model is sent to the regions that serve it, and no others.
        fix = f"set bedrock_region to {_spoken(served[key], 'or')}, or pin openai_region"
        assert fix in sentence


def test_a_pin_outside_the_served_regions_is_told_to_move(aws_home, mode_layer):
    layer, _downloaded = mode_layer
    layer["chat_models"] = {"gpt6-sol": {"openai_region": "us-west-2"}}
    row = doctor._gpt_region_check()
    assert row["status"] == "warn"
    assert _named(row["detail"]) == {"gpt6-sol"}
    assert (
        "resolves to us-west-2: change openai_region on its chat_models entry to" in (row["detail"])
    )


def test_the_region_row_is_part_of_the_report(aws_home, stub_bedrock, mode_layer, monkeypatch):
    _mode(monkeypatch, "cloud")
    layer, _downloaded = mode_layer
    layer["bedrock_region"] = "eu-west-1"
    row = _row("GPT regions")
    assert row["status"] == "warn" and "eu-west-1" in row["detail"]
