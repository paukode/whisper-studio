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


def engine_catalog(mode: str | None = None) -> list[dict]:
    """Rows for the settings pickers, filtered by the active model mode.

    cloud/hybrid: cloud Haiku plus every on-device model from the config, with
    download state so the UI can badge first-pick downloads — a cloud-mode user
    may still index with a local model, and picking one downloads it.

    local: on-device models ONLY, and only those already downloaded. In local
    mode nothing may call Bedrock, so offering Haiku would hand the user an
    engine that cannot run; offering a not-yet-downloaded model would hide a
    multi-gigabyte download behind a folder setting. An empty list is a real
    answer (a fresh install ships no local model) — the UI says to download one.
    """
    from server.infrastructure import model_mode as mm
    from server.local import runtime as local_rt

    active = mode or mm.current_mode()
    local_only = active == "local"

    rows: list[dict] = []
    if not local_only:
        rows.append(
            {"key": "haiku", "label": "Amazon Bedrock (Haiku)", "local": False, "downloaded": True}
        )
    for key, meta in local_registry.local_models().items():
        downloaded = bool(local_rt.is_downloaded(key))
        if local_only and not downloaded:
            continue
        rows.append(
            {
                "key": key,
                "label": meta.get("label") or key,
                "local": True,
                "downloaded": downloaded,
            }
        )
    return rows
