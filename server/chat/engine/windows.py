"""Context-window accounting: how many input tokens a model can take.

The engine budgets compaction and prompt-too-long rescue against the model's
real input cap instead of a hardcoded char guess. Resolution order:

  1. ``context_window`` on the model's chat_models entry (config-authoritative;
     the example config carries the known values, e.g. 278,528 for the GPT-5.6
     mantle deployments).
  2. A per-family default, so a model added without the field still budgets
     sanely.
  3. Local models: the live requested n_ctx (the composer's CTX chip is the
     source of truth), never a static number.

``prompt_cap_tokens`` parsed from a provider rejection (see events.RoundError)
always beats all of these for the retry — it is the deployment's own answer.
"""

import re

# Known input caps by provider family. Anthropic Claude on Bedrock exposes a
# 200K window; the OpenAI deployments on bedrock-mantle reject above 278,528
# prompt tokens (observed live from the deployment's own error).
FAMILY_DEFAULTS = {
    "anthropic": 200_000,
    "openai_bedrock": 278_528,
}
_FALLBACK = 200_000

# Leave room under the raw cap for the reply and counting drift: budgets
# derived from a window use this fraction of it.
SAFETY_FRACTION = 0.85

_CAP_RE = re.compile(
    r"exceed[s]?\D*model maximum\D*(\d[\d,]*)|maximum\D*(\d[\d,]*)\s*tokens?", re.I
)


def context_window(model_key: str, meta: dict | None = None) -> int | None:
    """The model's input cap in tokens, or None when only the live runtime
    knows (local models: ask the local runtime for the requested n_ctx)."""
    from server.infrastructure.config import load_config

    m = meta or (load_config().get("chat_model_meta") or {}).get(model_key) or {}
    if m.get("context_window"):
        return int(m["context_window"])
    if m.get("is_local"):
        # Live truth first: the running llama-server's actual n_ctx, then the
        # sticky requested size (composer CTX chip), then the registry ctx.
        try:
            from server.local import llama_server
            from server.local.runtime import requested_n_ctx

            n = llama_server.resident_n_ctx() or requested_n_ctx()
            if n:
                return int(n)
        except Exception:  # noqa: BLE001 — best-effort resolution
            pass
        return int(m["ctx"]) if m.get("ctx") else None
    return FAMILY_DEFAULTS.get(m.get("provider") or "anthropic", _FALLBACK)


def usable_tokens(window: int | None) -> int | None:
    """The share of a window the engine lets the prompt grow into."""
    return int(window * SAFETY_FRACTION) if window else None


def parse_prompt_cap(error_message: str) -> int | None:
    """Pull the deployment's input cap out of a prompt-too-long rejection,
    e.g. ``prompt tokens (281096) exceed customer model maximum (278528)``.
    Returns None when the message carries no usable number."""
    m = _CAP_RE.search(error_message or "")
    if not m:
        return None
    raw = next((g for g in m.groups() if g), None)
    if not raw:
        return None
    try:
        cap = int(raw.replace(",", ""))
    except ValueError:
        return None
    # Sanity: real caps are five digits or more; tiny numbers are misparses.
    return cap if cap >= 10_000 else None
