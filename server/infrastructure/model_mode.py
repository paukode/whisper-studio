"""Model mode + per-capability backend resolution.

A single ``model_mode`` decides where each indexing/RAG capability runs:

  - ``"cloud"``: all Amazon Bedrock (embed=cohere, rerank=cohere, ner=haiku,
    index_llm=haiku)
  - ``"local"``: all on-device (embed=qwen3, rerank=qwen3, ner=gliner,
    index_llm=local)
  - ``"hybrid"``: per-capability, read from the config ``backends`` map; any
    capability left unset falls back to the cloud backend.

Chat-model routing follows a model's own ``provider`` marker. The mode decides
which chat models are offered (``visible_chat_keys``) and, in local mode, which
may run at all (``turn_model_refusal``). The per-capability resolution covers
the four index/RAG capabilities.

The per-capability resolution here is the single source of truth the
embedder/reranker/NER factories read. The per-index backend stamp records what
ACTUALLY ran, so a mode flip queues a rebuild only where the stamp differs.
"""

from __future__ import annotations

MODES = ("cloud", "hybrid", "local")
CAPABILITIES = ("embed", "rerank", "ner", "index_llm")

# Canonical backend per capability for the two pure modes.
_CLOUD: dict[str, str] = {
    "embed": "cohere",
    "rerank": "cohere",
    "ner": "haiku",
    "index_llm": "haiku",
}
_LOCAL: dict[str, str] = {
    "embed": "qwen3",
    "rerank": "qwen3",
    "ner": "gliner",
    "index_llm": "local",
}

DEFAULT_MODE = "cloud"


def _cfg(config: dict | None) -> dict:
    if config is not None:
        return config
    from server.infrastructure.config import load_config

    return load_config()


def current_mode(config: dict | None = None) -> str:
    """The active model mode, coerced to a known value (default ``cloud``)."""
    mode = _cfg(config).get("model_mode") or DEFAULT_MODE
    return mode if mode in MODES else DEFAULT_MODE


def resolve_backend(capability: str, config: dict | None = None) -> str:
    """Backend name for ``capability`` under the active mode.

    cloud/local map every capability to their canonical backend; hybrid reads
    the per-capability ``backends`` override and falls back to the cloud backend
    when a capability is unset.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    cfg = _cfg(config)
    mode = current_mode(cfg)
    if mode == "local":
        return _LOCAL[capability]
    if mode == "cloud":
        return _CLOUD[capability]
    # hybrid: per-capability picks, default to the cloud backend when unset.
    overrides = cfg.get("backends") or {}
    chosen = overrides.get(capability)
    return chosen if isinstance(chosen, str) and chosen else _CLOUD[capability]


def visible_chat_keys(model_keys, meta: dict, mode: str) -> list:
    """Filter chat-model keys for the model picker by mode.

    cloud hides on-device models (no local runtime to serve them); local hides
    cloud models (everything runs on-device); hybrid shows all. The result can
    be empty, for example local mode before any on-device model is installed:
    the picker then shows an install hint rather than offering models the mode
    promised not to use.
    """
    keys = list(model_keys)
    if mode == "hybrid":
        return keys
    if mode == "local":
        return [k for k in keys if meta.get(k, {}).get("is_local")]
    return [k for k in keys if not meta.get(k, {}).get("is_local")]


def mode_default_model(model_keys, meta: dict, mode: str, configured: str) -> str:
    """The chat model a request with no explicit model runs on.

    The configured ``default_chat_model`` when the mode offers it, otherwise the
    first model the mode does offer, otherwise ``""`` (nothing is runnable, for
    example local mode with no on-device model). ``/api/models`` and the chat
    turn both resolve through here, so the picker and the server never disagree
    about the default.
    """
    visible = visible_chat_keys(model_keys, meta, mode)
    if configured in visible:
        return configured
    return visible[0] if visible else ""


NO_LOCAL_MODEL_REASON = (
    "Local mode runs chat on an on-device model, and none is installed. Install one "
    "from Settings > Models > Discover, or switch Settings > Model mode to Hybrid or "
    "Cloud to use cloud models."
)

NO_LOCAL_AGENT_MODEL_REASON = (
    "Local mode runs agents on an on-device model that supports tool calling, and "
    "none is installed. Install one from Settings > Models > Discover, or switch "
    "Settings > Model mode to Hybrid or Cloud to use cloud models."
)


def chat_only_agent_refusal(label: str, mode: str) -> str:
    """Why an on-device model without tool calling cannot run an agent, with
    a remedy the active mode can act on: Local mode never offers a cloud
    model, so it points to Discover and names cloud models only with the mode
    switch."""
    head = f"On-device model '{label}' does not support tool calling, so it cannot run agents."
    if mode == "local":
        return (
            f"{head} Pick a tool-capable on-device model, or install one from Settings > "
            "Models > Discover. To use cloud models, switch Settings > Model mode to "
            "Hybrid or Cloud."
        )
    return f"{head} Pick a tool-capable local model or a cloud model."


def on_device_schema_refusal(mode: str) -> str:
    """Why an agent on an on-device model cannot return structured output
    (its distill step runs on a cloud model), worded for the active mode."""
    if mode == "local":
        return (
            "Structured output (schema) needs a cloud model, and Local mode calls none. "
            "Run this agent without a schema, or switch Settings > Model mode to Hybrid "
            "or Cloud."
        )
    return (
        "Structured output (schema) requires a cloud model; run this agent without a "
        "schema or override the model with a cloud one."
    )


def turn_model_refusal(model_key: str, *, on_device: bool, label: str, mode: str) -> str | None:
    """A user-facing reason a turn on ``model_key`` must not run, or None.

    Local mode promises that nothing leaves this Mac, so a cloud model is
    refused at execution rather than sent to Bedrock. ``on_device`` must come
    from the runtime's own dispatch check (``server.local.runtime.
    is_local_model``), the same test that decides whether a turn goes to the
    local runtime, so this refusal and the dispatch can never disagree.
    """
    if not model_key:
        if mode == "local":
            return NO_LOCAL_MODEL_REASON
        return "No chat model is available. Enable one in Settings > Models."
    if mode != "local" or on_device:
        return None
    return (
        f"Local mode keeps chat on this Mac, so {label or model_key} (a cloud model) "
        "was not called. Pick an on-device model, or install one from Settings > "
        "Models > Discover. To use cloud models, switch Settings > Model mode to "
        "Hybrid or Cloud."
    )
