"""Executors for config_get / config_set."""

import json


def _mask_secrets(config: dict) -> dict:
    """Return a shallow copy of ``config`` with sensitive values masked.

    Any branch that serializes config into the tool result (and thus into the
    model context and sessions.db) must go through this — masking only the
    list-all branch let ``config_get(keys=["tavily_api_key"])`` leak the raw
    key. Masks to first-4-chars + ``***``.
    """
    result = dict(config)
    if result.get("tavily_api_key"):
        k = result["tavily_api_key"]
        result["tavily_api_key"] = k[:4] + "***" if len(k) > 4 else "***"
    return result


def execute_config_get(tool_input: dict) -> str:
    from server.infrastructure.config import load_config

    config = load_config()
    keys = tool_input.get("keys", [])
    if keys:
        result = {k: config.get(k) for k in keys}
    else:
        result = dict(config)
    # Mask secrets on BOTH branches before serialization.
    return json.dumps(_mask_secrets(result))


def execute_config_set(tool_input: dict) -> str:
    # Base the write on the RAW on-disk config, exactly like update_config in
    # server/infrastructure/config.py. load_config() returns the fully merged
    # view — chat_models flattened to {key: id}, a derived chat_model_meta key,
    # all DEFAULTS materialized, project overlay, and the env TAVILY_API_KEY
    # overlay. Persisting that would destroy the rich chat_models shape on disk
    # (label/thinking/provider/effort_tier/is_local/requires_data_retention),
    # bake chat_model_meta into the file, and write an env secret to disk.
    from server.infrastructure.config import DEFAULTS, _load_user_config, save_config

    updates = tool_input.get("updates", {})
    raw = _load_user_config()
    changed = []
    for key, val in updates.items():
        if key in DEFAULTS:
            raw[key] = val
            changed.append(key)
    save_config(raw)
    return json.dumps({"updated": changed, "ignored": [k for k in updates if k not in DEFAULTS]})
