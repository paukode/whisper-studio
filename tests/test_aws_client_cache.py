"""Cached boto3 clients never pin a missing credential.

botocore resolves credentials once, at client creation. Every process-wide
client cache (chat, memory recall, the data-retention control plane, Cohere)
must therefore cache a client only when it was built WITH credentials, so a
user who runs ``aws configure`` after the first failed turn is not stuck until
a relaunch. The environment is pinned to an empty temp AWS home; nothing here
opens a socket (client creation never does, and instance metadata is off).
"""

from __future__ import annotations

import os

import pytest

from server.chat import infra
from server.index import embedder_cohere
from server.infrastructure import data_retention
from server.memory import recall

GETTERS = {
    "chat": (infra._get_bedrock_client, infra._BEDROCK_CLIENTS),
    "recall": (lambda: recall._get_recall_client("us-east-1"), recall._RECALL_CLIENTS),
    "data_retention": (data_retention._get_control_plane_client, data_retention._clients),
    "cohere": (embedder_cohere._bedrock, embedder_cohere._clients),
}


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
    for _getter, cache in GETTERS.values():
        cache.clear()
    yield tmp_path
    for _getter, cache in GETTERS.values():
        cache.clear()


def _signing_credentials(client):
    return client._request_signer._credentials


@pytest.mark.parametrize("name", sorted(GETTERS))
def test_client_built_before_credentials_exist_is_not_pinned(aws_home, name):
    getter, cache = GETTERS[name]
    first = getter()
    assert _signing_credentials(first) is None
    assert not cache, "a client without credentials must not be cached"

    # The user runs `aws configure` while the app keeps running.
    (aws_home / "credentials").write_text(
        "[default]\naws_access_key_id = AKIDEXAMPLE\naws_secret_access_key = example-secret\n"
    )
    second = getter()
    assert _signing_credentials(second).access_key == "AKIDEXAMPLE"
    # Once built with credentials, the client is shared again.
    assert getter() is second


def test_a_bedrock_api_key_counts_as_credentials(aws_home, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "example-bedrock-api-key")
    getter, _cache = GETTERS["chat"]
    # Without this the fd-conserving cache would rebuild a client per call.
    assert getter() is getter()


def test_cohere_runs_in_the_settings_region_and_follows_a_change(aws_home, monkeypatch):
    # Cohere embed and rerank use the Bedrock region from Settings like every
    # other Bedrock call; changing it moves them without a relaunch.
    import json

    from server.infrastructure import config as cfg

    (aws_home / "credentials").write_text(
        "[default]\naws_access_key_id = AKIDEXAMPLE\naws_secret_access_key = example-secret\n"
    )
    layer = {"bedrock_region": "us-west-2"}
    monkeypatch.setattr(cfg, "_load_user_config", lambda: json.loads(json.dumps(layer)))
    cfg._invalidate_cache()
    try:
        west = embedder_cohere._bedrock()
        assert west.meta.region_name == "us-west-2"

        layer["bedrock_region"] = "us-east-2"
        cfg._invalidate_cache()
        east = embedder_cohere._bedrock()
        assert east.meta.region_name == "us-east-2"
        assert embedder_cohere._bedrock() is east
    finally:
        cfg._invalidate_cache()
