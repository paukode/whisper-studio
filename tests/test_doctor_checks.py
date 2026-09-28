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
    for name in ("AWS credentials", "Bedrock connectivity"):
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
