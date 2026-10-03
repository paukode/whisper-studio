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
* A LOCAL (on-device) key is honoured only when the model supports tool
  calling (the same registry gate interactive chat uses); a chat-only local
  model is refused up front with a readable reason instead of being handed
  an agent loop it cannot drive.
* In Local mode a CLOUD key is refused the same way (nothing leaves this
  Mac), with the reason chat turns give (model_mode.turn_model_refusal).
  run_agent refuses a cloud default too (runtime_support._resolve_agent_model),
  so the fallback can never reach Bedrock either.

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

        cfg = load_config()
        models = cfg.get("chat_models", {}) or {}
    except Exception:
        return (default_model_id or None), "", ""

    resolved = models.get(key)
    alias_note = ""
    if not resolved:
        # Family alias: the model asks for "sonnet" / "opus" / "haiku" while
        # the catalog keys are versioned ("sonnet5.0"). Seen 14 times in one
        # log, each silently downgraded to the default model. Resolve to the
        # newest configured key in that family and say so in the warning, so
        # the override still never fails silently. Keys with the same version
        # ("gpt6-astra", "gpt6-sol", "gpt6-luna" are all version 6) go to the
        # FIRST one listed: the catalog lists each family's flagship first, so
        # a tie never goes to whichever smaller sibling was added last. A newer
        # version wins outright: "gpt6" means GPT-6.1 Sol ("gpt6.1-sol").
        family = [k for k in models if k.lower().startswith(key.lower())]
        if family:
            key = max(family, key=_version_sort_key)
            resolved = models.get(key)
            alias_note = f"model alias resolved to '{key}'"
    if not resolved:
        return (
            (default_model_id or None),
            "",
            f"unknown model '{key}'; ran on the default model instead",
        )

    from server.infrastructure.model_mode import current_mode, turn_model_refusal
    from server.local.runtime import is_local_model_id, supports_tools

    if current_mode(cfg) == "local" and not is_local_model_id(resolved):
        label = ((cfg.get("chat_model_meta") or {}).get(key) or {}).get("label", "")
        refusal = turn_model_refusal(key, on_device=False, label=label, mode="local")
        return (
            (default_model_id or None),
            "",
            f"{refusal} The agent ran on the default model instead.",
        )
    if is_local_model_id(resolved) and not supports_tools(key):
        return (
            (default_model_id or None),
            "",
            (
                f"model '{key}' is on-device and does not support tool "
                "calling, so it cannot run agents; ran on the default "
                "model instead"
            ),
        )
    return resolved, key, alias_note


def _version_sort_key(key: str) -> tuple:
    """The version right after the family name, wherever the key puts it:
    "sonnet5.0" -> (5, 0), "gpt5.6-sol" -> (5, 6), "gpt6-astra" -> (6,). A
    key with no version ("haiku", "sonnet") sorts lowest."""
    import re

    m = re.search(r"\d+(?:\.\d+)*", key)
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(0).split("."))
