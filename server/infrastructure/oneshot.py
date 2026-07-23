"""Mode-aware one-shot (non-streaming) LLM completion.

A single place to run one prompt through one completion in a way that works in
cloud, hybrid, and local model modes. The workspace-index modules each grew
their own private copy of this (server/index/contextualize.py, descriptions.py,
relations.py), but those are best-effort and swallow every failure to ``""``.
This helper is for user-facing callers (the transcript map-reduce condenser)
and RAISES on hard failure so the caller can fall back explicitly instead of
silently producing an empty completion.
"""

import json
import logging

log = logging.getLogger("whisper-studio")

_LOCAL_KEY = "local_gemma"


def _resolve_local_key(local_model_key: str | None) -> str:
    """Pick which on-device model runs the local map step.

    Prefer, in order: the caller's key (the active chat model), else the model
    currently resident in the runtime, else the first downloaded model in the
    catalogue, else the default. Following the active/resident model matters
    because only one ~7GB model is resident at a time: pinning a fixed key would
    evict the resident chat model, run the map, then reload the chat model — two
    multi-GB load/unload cycles per turn. It also lets a coder-only install
    condense instead of failing because the default key is not downloaded.

    The resolved key is only a candidate; :func:`one_shot` still requires it to be
    downloaded before calling ``complete`` so a summary never triggers a silent
    multi-GB download.
    """
    from server.local import runtime as local_rt

    if local_model_key and local_rt.is_local_model(local_model_key):
        return local_model_key
    resident = local_rt.loaded_key()
    if resident and local_rt.is_local_model(resident):
        return resident
    for k in local_rt.LOCAL_MODELS:
        if local_rt.is_downloaded(k):
            return k
    return _LOCAL_KEY


def resolve_map_engine(config: dict | None = None) -> str:
    """Return the engine (``"haiku"`` or ``"local"``) for a one-shot map call
    under the active model mode. Anything the mode resolver returns outside that
    set (e.g. a hybrid ``"none"``) is coerced to ``"haiku"``: the map step must
    always produce output, and ``"none"`` only means "skip optional index
    enrichment", not "cannot summarise"."""
    from server.infrastructure.model_mode import resolve_backend

    engine = resolve_backend("index_llm", config)
    return engine if engine in ("haiku", "local") else "haiku"


def _is_claude_id(model_id: str) -> bool:
    """True for a plain Claude Bedrock id that accepts the Anthropic invoke_model
    body. Excludes local ids, openai-provider ids, and the data-retention gated
    Fable model."""
    mid = (model_id or "").lower()
    if not mid or mid.startswith("local:") or mid.startswith("openai."):
        return False
    if "fable" in mid:
        return False
    return "claude" in mid


def _pick_claude_fallback(models: dict) -> str | None:
    """Pick a Claude model id when the requested key is missing: prefer the
    configured default if it is a Claude id, else the first Claude id in the
    catalogue, else None (caller raises)."""
    from server.chat.infra import _get_default_model

    default_id = models.get(_get_default_model())
    if _is_claude_id(default_id):
        return default_id
    for mid in models.values():
        if _is_claude_id(mid):
            return mid
    return None


def one_shot(
    system: str,
    user: str,
    *,
    max_tokens: int,
    engine: str | None = None,
    cloud_model_key: str = "haiku",
    local_model_key: str | None = None,
) -> str:
    """Run one system+user prompt through a single completion and return the
    assistant text.

    Non-streaming and blocking (safe from a worker thread; wrap in
    ``run_in_executor`` when calling from the event loop). Raises on hard
    failure (no cloud model id, local model not downloaded, transport error, or
    an empty result) so callers can fall back. ``engine`` defaults to
    :func:`resolve_map_engine`. Only the Anthropic body shape is supported, so
    the cloud model must be a Claude id (never a gpt/fable key).

    ``local_model_key`` names the on-device model for a local map call; when
    unset the resolver follows the resident/first-downloaded model (see
    :func:`_resolve_local_key`) instead of a fixed key, so the map does not evict
    the active chat model.
    """
    engine = engine or resolve_map_engine()

    if engine == "local":
        from server.local import runtime as local_rt

        key = _resolve_local_key(local_model_key)
        # complete() would download multi-GB weights if the model is absent;
        # never let a summary request trigger that silently.
        if not local_rt.is_downloaded(key):
            raise RuntimeError(f"local map model {key!r} is not downloaded")
        out = local_rt.complete(key, system, user, max_tokens=max_tokens)
        if not out:
            raise RuntimeError("local one-shot completion returned empty")
        return out

    # cloud / haiku
    from server.chat.infra import _get_bedrock_client, _get_chat_models

    models = _get_chat_models()
    model_id = models.get(cloud_model_key)
    if not model_id:
        # A config.json can drop the haiku key. Fall back to another Claude model
        # rather than failing outright, but NEVER to a gpt/fable/local id: the
        # body below is Anthropic-shaped, so a gpt id 400s on bedrock-runtime, a
        # local id is not a Bedrock model at all, and Fable is a data-retention
        # gated model that must not be handed the transcript here.
        model_id = _pick_claude_fallback(models)
        log.warning(
            "one_shot: cloud model %r not configured; using Claude fallback %r",
            cloud_model_key,
            model_id,
        )
    if not model_id:
        raise RuntimeError("no Claude cloud model id available for one-shot completion")

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    )
    resp = _get_bedrock_client().invoke_model(modelId=model_id, body=body)
    payload = json.loads(resp["body"].read())
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    if not text:
        raise RuntimeError("cloud one-shot completion returned no text")
    return text
