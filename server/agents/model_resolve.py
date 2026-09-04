"""Resolve a model-override KEY (a chat_models config key, e.g. 'sonnet') to a
model id, shared by every agent spawn surface: a workflow script's
``agent(..., {model})`` opt (server/workflows/agent_adapter.py) and the
spawn_agent tool's ``model`` parameter (server/agent_tools/spawn.py).

Returns ``(model_id, effective_key, warning)``. Two override failure modes are
reported instead of swallowed (they used to fail silently on the workflow
path, mispricing the ledger — see tests/test_model_resolution.py):

* An UNKNOWN key falls back to the caller's default model, and the empty
  effective_key tells the ledger to price under the default's own key rather
  than the invalid name.
* A LOCAL (on-device) key resolves to a ``local:*`` id the agent runtime has
  no adapter for; refused up front with a readable reason instead of being
  handed to the Bedrock adapter to fail at invoke.

``warning`` is empty when the override resolved cleanly (or no override was
asked for).
"""

from __future__ import annotations


def resolve_model_override(
    key: str | None, default_model_id: str | None
) -> tuple[str | None, str, str]:
    if not key:
        return (default_model_id or None), "", ""
    try:
        from server.infrastructure.config import load_config

        models = load_config().get("chat_models", {}) or {}
    except Exception:
        return (default_model_id or None), "", ""

    resolved = models.get(key)
    if not resolved:
        return (
            (default_model_id or None),
            "",
            f"unknown model '{key}'; ran on the default model instead",
        )

    from server.local.runtime import is_local_model_id

    if is_local_model_id(resolved):
        return (
            (default_model_id or None),
            "",
            (
                f"model '{key}' is on-device and agents have no local "
                "adapter; ran on the default model instead"
            ),
        )
    return resolved, key, ""
