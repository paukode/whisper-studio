"""Executors for config_get / config_set."""

import json

from server.approval.markers import approval_request

# Config keys a write to which decides what the assistant may do next or where
# its data lives: the data root (``data_dir``), the default permission mode,
# the permission explainer, the feature flags, the auto-mode classifier rules,
# and the auxiliary-model routing (the classifier's own model). A config_set
# that touches one of these follows the permission mode (the app-config table
# in server/security/permissions.py, category "config"): a person decides it
# in every mode but bypassPermissions, and an unattended run is refused. Every
# other key (a language, a theme flag, a timezone) still writes with no card.
SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "data_dir",
        "permission_mode",
        "permission_explainer_enabled",
        "feature_flags",
        "auto_mode_allow",
        "auto_mode_soft_deny",
        "auto_mode_environment",
        "auxiliary_models",
    }
)


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


def _apply_config_updates(updates: dict) -> str:
    """Persist the config updates (the keys in DEFAULTS) and report what landed.

    Base the write on the RAW on-disk config, exactly like update_config in
    server/infrastructure/config.py. load_config() returns the fully merged
    view — chat_models flattened to {key: id}, a derived chat_model_meta key,
    all DEFAULTS materialized, project overlay, and the env TAVILY_API_KEY
    overlay. Persisting that would destroy the rich chat_models shape on disk
    (label/thinking/provider/effort_tier/is_local/requires_data_retention),
    bake chat_model_meta into the file, and write an env secret to disk.
    """
    from server.infrastructure.config import (
        DEFAULTS,
        USER_CONFIG_LOCK,
        _load_user_config,
        save_config,
    )

    with USER_CONFIG_LOCK:  # one read-modify-write, no writer in between
        raw = _load_user_config()
        changed = []
        for key, val in updates.items():
            if key in DEFAULTS:
                raw[key] = val
                changed.append(key)
        save_config(raw)
    return json.dumps({"updated": changed, "ignored": [k for k in updates if k not in DEFAULTS]})


def execute_config_set(tool_input: dict) -> str:
    updates = tool_input.get("updates", {})
    # A write to a sensitive key (SENSITIVE_CONFIG_KEYS: the data root, the
    # permission mode, the classifier rules) follows the permission mode: the
    # gate (category "config") asks in every mode but bypass, denies in
    # dontAsk, and refuses an unattended run. The actual write runs in
    # _do_config_set once the mode (or a person) allows it. Any other key
    # (a language, a flag with no security weight) writes straight away.
    if isinstance(updates, dict) and any(k in SENSITIVE_CONFIG_KEYS for k in updates):
        return approval_request({"action": "config_set", "updates": updates})
    return _apply_config_updates(updates)


async def _do_config_set(payload: dict):
    """The gate's executor for a sensitive config_set (the card's Yes, or a
    mode that allowed it). Refused from an unattended agent: moving the data
    root or changing the permission mode needs a person."""
    from server.approval.spec import ApprovalOutcome, refuse_if_agent

    refusal = refuse_if_agent(payload, what="A change to the app's config")
    if refusal:
        return refusal
    updates = payload.get("updates") or {}
    if not isinstance(updates, dict):
        return ApprovalOutcome(ok=False, error="config_set updates must be an object")
    return ApprovalOutcome(ok=True, output=_apply_config_updates(updates))


def _config_set_summary(payload: dict) -> str:
    updates = payload.get("updates") or {}
    keys = ", ".join(sorted(k for k in updates)) if isinstance(updates, dict) else "?"
    return f"Change app config: {keys}"


def register_config_approval() -> None:
    """The ApprovalSpec for a sensitive config_set (category "config"), called
    from server/approval/bootstrap.py's register_defaults()."""
    from server.approval.registry import register
    from server.approval.spec import ApprovalSpec

    register(
        "config_set",
        ApprovalSpec(
            category="config",
            preview="text",
            summary=_config_set_summary,
            executor=_do_config_set,
            risk_hint="high",
            payload_fields=["updates"],
        ),
    )
