"""Shared engine resolution for the index's LLM passes.

The per-workspace settings store an ``engine`` string for relationship
mapping (``typed_relations``), entity descriptions, and AI-written chunk
headers. Valid LLM engine values:

- ``"haiku"`` — cloud, Bedrock Claude Haiku
- any on-device model key from the local registry (``local_gemma``,
  ``local_qwen35_9b``, ...), sourced from ``chat_models`` in the config
- ``"local"`` — "follow the available on-device model": the preferred default
  (``local_gemma``) when it is installed, else the first installed local model.
  Accepted so folder settings written before the model picker existed keep
  working, and so ``local`` mode resolves even though the app ships no local
  model by default (the user installs one from Discover).

``"none"`` and ``"gliner2"`` are pass-specific sentinels owned by the callers
(wssettings validates them); they are not LLM engines.
"""

from server.local import registry as local_registry

LEGACY_LOCAL_ALIAS = "local"
_PREFERRED_LOCAL_KEY = "local_gemma"


def resolve_local_model(engine: str) -> str | None:
    """The on-device model key an engine value runs on, or ``None`` when the
    value is not a local engine ("haiku", "none", "gliner2", unknown) OR when it
    is the ``"local"`` alias but no on-device model is installed yet."""
    models = local_registry.local_models()
    if engine == LEGACY_LOCAL_ALIAS:
        if _PREFERRED_LOCAL_KEY in models:
            return _PREFERRED_LOCAL_KEY
        return next(iter(models), None)  # first installed local model, or None
    if engine in models:
        return engine
    return None


def is_llm_engine(engine: str) -> bool:
    """True for values the LLM passes can actually run: haiku or a local model."""
    return engine == "haiku" or resolve_local_model(engine) is not None


def engine_catalog() -> list[dict]:
    """Rows for the settings pickers: cloud Haiku plus every on-device model,
    with download state so the UI can badge first-pick downloads. Deliberately
    NOT filtered by model_mode — the index engines are a separate choice from
    the chat model, and a cloud-mode user may still index with local Gemma."""
    from server.local import runtime as local_rt

    rows: list[dict] = [
        {"key": "haiku", "label": "Amazon Bedrock (Haiku)", "local": False, "downloaded": True}
    ]
    for key, meta in local_registry.local_models().items():
        rows.append(
            {
                "key": key,
                "label": meta.get("label") or key,
                "local": True,
                "downloaded": bool(local_rt.is_downloaded(key)),
            }
        )
    return rows
