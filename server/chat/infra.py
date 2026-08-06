"""Bedrock client cache + model-config accessors.

This module owns the process-wide singleton boto3 client (one per region)
so the agent runtime and the chat endpoint share the same connection pool.
Creating a fresh boto3 client per Bedrock call ate file descriptors fast
enough to trigger [Errno 24] under heavy parallel team spawns — see
``_get_bedrock_client``'s comment for the full backstory.

Model accessors live here too because they're both thin config wrappers
that the rest of the chat package leans on. Keeping them together keeps
the dependency graph shallow.
"""

import threading

import boto3
from botocore.config import Config as BotoConfig

from server.costs.tracker import estimate_cost as _estimate_cost_fn
from server.infrastructure.config import load_config

# Single shared bedrock client per region. boto3 clients own a
# connection pool and are thread-safe — creating a fresh client on
# every invoke multiplies the pool count by the number of concurrent agents and
# can exhaust the macOS default soft fd limit (256). That cascade
# is what causes [Errno 24] Too many open files, then sqlite
# "unable to open database file", then "Could not connect to
# bedrock-runtime…" — the resolver itself can't open a socket once
# fds are gone.
_BEDROCK_CLIENTS: dict[str, object] = {}
_BEDROCK_CLIENT_LOCK = threading.Lock()


def _get_bedrock_client():
    config = load_config()
    region = config.get("bedrock_region", "us-east-1")
    with _BEDROCK_CLIENT_LOCK:
        client = _BEDROCK_CLIENTS.get(region)
        if client is None:
            client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=BotoConfig(
                    read_timeout=600,
                    connect_timeout=10,
                    retries={"max_attempts": 2},
                    # Bound the pool so a parallel team spawn can't
                    # fan out into hundreds of sockets. 32 is well
                    # above the executor sizes that share it.
                    max_pool_connections=32,
                ),
            )
            _BEDROCK_CLIENTS[region] = client
        return client


def _reset_bedrock_client_cache() -> None:
    """Test hook + config-change escape hatch — drops cached clients
    so the next ``_get_bedrock_client()`` picks up fresh settings
    (e.g. after the user changes region in Settings)."""
    with _BEDROCK_CLIENT_LOCK:
        _BEDROCK_CLIENTS.clear()


def _downloaded_local_only() -> dict:
    """On-device models present in the registry (downloaded) but NOT in config
    chat_models. The app ships no local chat models, so a recommended model the
    user downloaded — or one left on disk by a prior release — has no config
    entry; it must still be selectable and routable. The registry is
    disk-authoritative for local models (only surfaces downloaded recommendations
    plus config is_local entries), so this yields exactly the downloaded ones the
    config doesn't already carry. Never raises: a registry hiccup must not break
    the chat catalog."""
    try:
        from server.local.registry import local_models

        cfg_keys = set(load_config().get("chat_models", {}))
        return {k: m for k, m in local_models().items() if k not in cfg_keys}
    except Exception:  # pragma: no cover - defensive
        return {}


def _get_chat_models() -> dict:
    models = dict(load_config().get("chat_models", {}))
    for key, m in _downloaded_local_only().items():
        models.setdefault(key, m.get("id") or f"local:{key}")
    return models


def _get_chat_model_meta() -> dict:
    """Per-model metadata (label, thinking mode). Sibling of chat_models —
    populated by infrastructure.config._normalize_chat_models from the same
    rich shape on disk. Empty dict if config.json hasn't been loaded yet.
    Downloaded local models without a config entry are folded in with derived
    meta so the picker can badge them and route selection through the local
    runtime."""
    meta = dict(load_config().get("chat_model_meta", {}))
    for key, m in _downloaded_local_only().items():
        meta.setdefault(
            key,
            {
                "id": m.get("id") or f"local:{key}",
                "label": m.get("label") or key,
                "is_local": True,
                "supports_thinking": bool(m.get("supports_thinking")),
                "supports_tools": bool(m.get("supports_tools")),
            },
        )
    return meta


def _get_default_model() -> str:
    config = load_config()
    return config.get("default_chat_model", "opus4.6")


def _estimate_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cached_in_input: bool = False,
) -> float:
    """Estimate USD cost for a model call using the cost tracker's pricing,
    including prompt-cache read/write tokens (both default 0).

    ``cached_in_input``: OpenAI reports cached tokens as a subset of
    input_tokens (bill the cached share once at the cache-read rate);
    Anthropic's buckets are disjoint (the default)."""
    return _estimate_cost_fn(
        model_key,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        cached_in_input=cached_in_input,
    )
