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
# every invoke (which the agent runtime + team executor used to do)
# multiplies the pool count by the number of concurrent agents and
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


def _get_chat_models() -> dict:
    return load_config().get("chat_models", {})


def _get_chat_model_meta() -> dict:
    """Per-model metadata (label, thinking mode). Sibling of chat_models —
    populated by infrastructure.config._normalize_chat_models from the same
    rich shape on disk. Empty dict if config.json hasn't been loaded yet."""
    return load_config().get("chat_model_meta", {})


def _get_default_model() -> str:
    config = load_config()
    return config.get("default_chat_model", "opus4.6")


def _estimate_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate USD cost for a Bedrock call using the cost tracker's pricing,
    including prompt-cache read/write tokens (both default 0)."""
    return _estimate_cost_fn(
        model_key,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    )
