"""Bedrock client cache + model-config accessors.

This module owns the process-wide singleton boto3 client (one per region)
so the agent runtime and the chat endpoint share the same connection pool.
Creating a fresh boto3 client per Bedrock call ate file descriptors fast
enough to trigger [Errno 24] under heavy parallel team spawns (see
``_get_bedrock_client``'s comment for the full backstory).

Model accessors live here too because they're both thin config wrappers
that the rest of the chat package leans on. Keeping them together keeps
the dependency graph shallow.
"""

import threading

from botocore.config import Config as BotoConfig

from server.infrastructure.aws_clients import cached_client
from server.infrastructure.config import DEFAULTS, load_config

# Single shared bedrock client per region. boto3 clients own a
# connection pool and are thread-safe; creating a fresh client on
# every invoke multiplies the pool count by the number of concurrent agents and
# can exhaust the macOS default soft fd limit (256). That cascade
# is what causes [Errno 24] Too many open files, then sqlite
# "unable to open database file", then "Could not connect to
# bedrock-runtime...", because the resolver itself can't open a socket once
# fds are gone. A client built before any AWS credentials existed is NOT
# cached (server/infrastructure/aws_clients.py), so adding credentials later
# takes effect on the next call instead of after a relaunch.
_BEDROCK_CLIENTS: dict[str, object] = {}
_BEDROCK_CLIENT_LOCK = threading.Lock()


def _get_bedrock_client():
    config = load_config()
    region = config.get("bedrock_region", "us-east-1")
    return cached_client(
        _BEDROCK_CLIENTS,
        _BEDROCK_CLIENT_LOCK,
        region,
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(
            read_timeout=600,
            connect_timeout=10,
            # "adaptive" (not the default legacy mode): it backs off AND
            # rate-limits client-side when Bedrock starts throttling. That is
            # what makes a 16-wide agent fan-out safe; without it, a burst that
            # trips the account quota fails the agents outright instead of
            # pacing them.
            retries={"max_attempts": 5, "mode": "adaptive"},
            # Bound the pool so a parallel team spawn can't fan out into
            # hundreds of sockets. 32 is well above the executor sizes that
            # share it.
            max_pool_connections=32,
        ),
    )


def _reset_bedrock_client_cache() -> None:
    """Test hook: drops cached clients so the next ``_get_bedrock_client()``
    builds fresh ones (tests/conftest.py resets between tests). Nothing in the
    config-update path calls this: clients are keyed per region, so a region
    change simply builds a new entry."""
    with _BEDROCK_CLIENT_LOCK:
        _BEDROCK_CLIENTS.clear()


def _downloaded_local_only(config: dict | None = None) -> dict:
    """On-device models present in the registry (downloaded) but NOT in config
    chat_models. The app ships no local chat models, so a recommended model the
    user downloaded (or one left on disk by a prior release) has no config
    entry; it must still be selectable and routable. The registry is
    disk-authoritative for local models (only surfaces downloaded recommendations
    plus config is_local entries), so this yields exactly the downloaded ones the
    config doesn't already carry. ``config`` is the catalog to compare against
    (a workspace-aware ``load_config(ws)``); omitted, the global config. Never
    raises: a registry hiccup must not break the chat catalog."""
    try:
        from server.local.registry import local_models

        cfg = load_config() if config is None else config
        cfg_keys = set(cfg.get("chat_models", {}))
        return {k: m for k, m in local_models().items() if k not in cfg_keys}
    except Exception:  # pragma: no cover - defensive
        return {}


def _get_chat_models(config: dict | None = None) -> dict:
    cfg = load_config() if config is None else config
    models = dict(cfg.get("chat_models", {}))
    for key, m in _downloaded_local_only(cfg).items():
        models.setdefault(key, m.get("id") or f"local:{key}")
    return models


def _get_chat_model_meta(config: dict | None = None) -> dict:
    """Per-model metadata (label, thinking mode). Sibling of chat_models,
    populated by infrastructure.config._normalize_chat_models from the same
    rich shape on disk. Empty dict if config.json hasn't been loaded yet.
    Downloaded local models without a config entry are folded in with derived
    meta so the picker can badge them and route selection through the local
    runtime. ``config`` as for ``_get_chat_models``."""
    cfg = load_config() if config is None else config
    meta = dict(cfg.get("chat_model_meta", {}))
    for key, m in _downloaded_local_only(cfg).items():
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


def mode_chat_catalog(cfg: dict) -> tuple[list[str], dict, str, str]:
    """What the model picker offers in the active mode, as
    ``(visible keys, meta, mode, default key)``: /api/models and /doctor read
    the same answer. Cloud hides on-device models, Local hides cloud ones, and
    the list (and the default) is empty in Local mode before an on-device
    model is installed."""
    from server.infrastructure.model_mode import (
        current_mode,
        mode_default_model,
        visible_chat_keys,
    )

    meta = _get_chat_model_meta(cfg)
    mode = current_mode(cfg)
    visible = visible_chat_keys(list(_get_chat_models(cfg)), meta, mode)
    default = mode_default_model(visible, meta, mode, cfg.get("default_chat_model", ""))
    return visible, meta, mode, default


def _get_default_model() -> str:
    config = load_config()
    return config.get("default_chat_model", DEFAULTS["default_chat_model"])


def _turn_catalog(session_config: dict, ws_path: str | None) -> tuple[dict, dict]:
    """``(chat_models, chat_model_meta)`` a chat turn resolves its model against.

    The session's latched entries, plus every key the LIVE workspace-aware
    catalog has that the latch lacks: a model installed from Discover after the
    session latched, or an on-device model that is on disk with no config entry
    (the latch snapshots ``load_config`` and never sees those). Latched entries
    win for keys both carry, so an existing session keeps its frozen ids and
    metadata; adding keys cannot fork a cached prefix, because the prefix only
    depends on the model the turn actually runs. A model the workspace hides is
    absent from the live catalog too, so it is not brought back here."""
    live_cfg = load_config(ws_path)
    models = {**_get_chat_models(live_cfg), **(session_config.get("chat_models") or {})}
    meta = {**_get_chat_model_meta(live_cfg), **(session_config.get("chat_model_meta") or {})}
    return models, meta


def effort_for_model(model_key: str, requested: str | None = None) -> str | None:
    """The effort label to run ``model_key`` at, resolved against its own
    capabilities.

    ``requested`` is the caller's level — a chat turn's, a workflow run's, a
    parent agent's. Omit it and the configured ``effort_level`` is used, which
    is what every unattended entry point (cron, headless) should do: they have
    no composer to read, but "no session" is not the same as "no effort", and
    passing None all the way down meant those turns sent no ``thinking`` block
    at all. ``None`` comes back only for models with no effort support (Haiku).
    """
    from server.infrastructure.effort import resolve_effort

    config = load_config()
    level = requested or config.get("effort_level") or None
    return resolve_effort(_get_chat_model_meta().get(model_key, {}), model_key, level)
