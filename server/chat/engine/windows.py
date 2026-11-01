"""Context-window accounting: how many input tokens a model can take.

The engine budgets compaction and prompt-too-long rescue against the model's
real input cap instead of a hardcoded char guess. Resolution order:

  1. ``context_window`` on the model's chat_models entry (config-authoritative;
     the example config carries the known values, e.g. 278,528 for the GPT
     mantle deployments).
  2. Local models: the resident server's n_ctx, then the requested n_ctx (the
     composer's CTX chip), then the model's own ctx (the local registry always
     sets one).
  3. A per-provider default, so a cloud model added without the field still
     budgets sanely. An unknown provider is an error.

``parse_prompt_cap`` pulls the deployment's own cap out of a prompt-too-long
rejection; the OpenAI adapter uses it to classify those errors as trimmable.
"""

import re

# The tuple lives in infrastructure.config, which also reads it while it
# normalizes chat_models during load_config().
from server.infrastructure.config import ANTHROPIC_1M_MARKERS

# Known input caps by provider family. VERIFIED 2026-08-29: Bedrock accepted a
# 312K-token prompt on global.anthropic.claude-sonnet-5 (invoke_model,
# max_tokens=1), so the old "Bedrock caps Claude at 200K" assumption is dead;
# per the platform-availability table, 1M context windows are GA on Bedrock
# for the 1M-family models below. Haiku 4.5 remains a 200K model. The OpenAI
# deployments on bedrock-mantle reject above 278,528 prompt tokens (observed
# live from the deployment's own error).
FAMILY_DEFAULTS = {
    "anthropic": 200_000,
    "openai_bedrock": 278_528,
}

# Fractions of the input budget where compaction acts: the proactive trigger
# and the hard ceiling a summary must fit under.
_TRIGGER_FRACTION = 0.95
_HARD_FRACTION = 0.98
_CHARS_PER_TOKEN = 4


def _wire_id(model_key: str, cfg: dict) -> str:
    """The wire model id for a chat_models key ('' when unknown). Local
    entries use the dict form; cloud entries are plain id strings."""
    v = (cfg.get("chat_models") or {}).get(model_key)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("id") or "")
    return ""


def anthropic_output_ceiling(model_id: str) -> int:
    """Bedrock rejects max_tokens above the model's output ceiling: Haiku caps
    at 64K ("max_tokens: 128000 > 64000" invalid-request errors), every other
    current Claude tier accepts 128K."""
    return 64_000 if "haiku" in (model_id or "").lower() else 128_000


_CAP_RE = re.compile(
    r"exceed[s]?\D*model maximum\D*(\d[\d,]*)|maximum\D*(\d[\d,]*)\s*tokens?", re.I
)


def context_window(model_key: str, meta: dict | None = None) -> int:
    """The model's context window in tokens. Raises ValueError when the model
    has no window: an on-device key that is not installed, or an unknown
    provider."""
    from server.infrastructure.config import load_config
    from server.local.runtime import LOCAL_MODELS, is_local_model

    cfg = load_config()
    m = meta or (cfg.get("chat_model_meta") or {}).get(model_key) or {}
    if m.get("context_window"):
        return int(m["context_window"])
    if m.get("is_local") or is_local_model(model_key):
        # Live truth first: the running model server's actual n_ctx, then the
        # sticky requested size (composer CTX chip), then the model's ctx.
        from server.local import serving
        from server.local.runtime import requested_n_ctx

        n = serving.resident_n_ctx() or requested_n_ctx()
        if n:
            return int(n)
        if m.get("ctx"):
            return int(m["ctx"])
        if not is_local_model(model_key):
            raise ValueError(
                f"On-device model {model_key!r} is not installed, so its context size is unknown."
            )
        return int(LOCAL_MODELS[model_key]["ctx"])
    provider = m.get("provider") or "anthropic"
    if provider == "anthropic":
        mid = _wire_id(model_key, cfg).lower()
        if any(marker in mid for marker in ANTHROPIC_1M_MARKERS):
            return 1_000_000
    if provider not in FAMILY_DEFAULTS:
        raise ValueError(f"Model {model_key!r} has an unknown provider {provider!r}.")
    return FAMILY_DEFAULTS[provider]


def output_reserve(model_key: str, meta: dict | None = None) -> int:
    """Tokens to hold back for the answer when budgeting input.

    The Anthropic window is TOTAL (input + max_tokens must fit), and a local
    model's n_ctx likewise covers prompt plus generation. The GPT mantle
    deployments enforce a separate PROMPT cap (that is what 278,528 is), so
    nothing is reserved there."""
    from server.infrastructure.config import load_config

    cfg = load_config()
    m = meta or (cfg.get("chat_model_meta") or {}).get(model_key) or {}
    if m.get("is_local"):
        try:
            return int(m.get("max_output") or 4096)
        except (TypeError, ValueError):
            return 4096
    if (m.get("provider") or "anthropic") == "openai_bedrock":
        return 0
    # Anthropic: mirror the adapter's max_tokens clamp exactly, so the budget
    # reserves what the request will actually ask for.
    ceiling = anthropic_output_ceiling(_wire_id(model_key, cfg))
    try:
        return min(int(m.get("max_output") or ceiling), ceiling)
    except (TypeError, ValueError):
        return ceiling


def input_budget(model_key: str, meta: dict | None = None) -> int:
    """Largest prompt (in tokens) a request for this model can safely carry:
    the context window minus the reserved answer, floored at a quarter of the
    window so a misconfigured max_output can never starve the prompt."""
    ctx = context_window(model_key, meta)
    return max(int(ctx) - output_reserve(model_key, meta), int(ctx) // 4)


def compact_thresholds_chars(model_key: str, meta: dict | None = None) -> tuple[int, int]:
    """Per-model compaction thresholds in chars: the proactive trigger at 95%
    of the input budget and the hard ceiling at 98%, at the codebase's
    4-chars/token estimate."""
    budget = input_budget(model_key, meta)
    chars = budget * _CHARS_PER_TOKEN
    return int(chars * _TRIGGER_FRACTION), int(chars * _HARD_FRACTION)


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
