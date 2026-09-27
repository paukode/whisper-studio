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

# The hybrid "One-shot writer: Off" value: no model runs the map step.
OFF = "none"


def resolve_local_key(local_model_key: str | None) -> str:
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
    """Return the engine for a one-shot map call under the active model mode:
    ``"haiku"``, ``"local"`` (follow the resident on-device model), a specific
    on-device model key from the local registry, or ``"none"`` when the user
    switched the hybrid one-shot writer Off. Off means no model runs the map
    step at all; the caller decides what happens to input that does not fit.
    An unknown value is coerced to ``"haiku"``, the hybrid cloud default
    :func:`~server.infrastructure.model_mode.resolve_backend` also uses for an
    unset capability."""
    from server.infrastructure.model_mode import resolve_backend
    from server.local import registry as local_registry

    engine = resolve_backend("index_llm", config)
    if engine in ("haiku", "local", OFF) or engine in local_registry.local_models():
        return engine
    return "haiku"


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
    feature: str = "This model call",
    source: str,
    session_id: str = "",
) -> str:
    """Run one system+user prompt through a single completion and return the
    assistant text.

    A cloud call is logged in the cost log under ``source`` (and
    ``session_id`` when it served one); an on-device call costs nothing and is
    not logged.

    Non-streaming and blocking (safe from a worker thread; wrap in
    ``run_in_executor`` when calling from the event loop). Raises on hard
    failure (no cloud model id, local model not downloaded, transport error, or
    an empty result) so callers can fall back. ``engine`` defaults to
    :func:`resolve_map_engine`. Cloud keys may be Claude (Bedrock Anthropic
    body) or GPT (OpenAI Responses on mantle); anything else falls back to a
    Claude model.

    ``local_model_key`` names the on-device model for a local map call; when
    unset the resolver follows the resident/first-downloaded model (see
    :func:`resolve_local_key`) instead of a fixed key, so the map does not evict
    the active chat model.

    In Local mode the cloud branch raises
    :class:`~server.infrastructure.cloud_guard.CloudRefused` (its message,
    naming ``feature``, is the user-facing reason) before any client is built.
    The ``"none"`` engine (the one-shot writer switched Off) raises too: Off
    never quietly becomes a cloud call.
    """
    engine = engine or resolve_map_engine()
    if engine == OFF:
        raise RuntimeError(
            "the one-shot writer is Off (Settings > Model mode), so no model runs this call"
        )

    from server.local import runtime as local_rt

    if engine == "local" or local_rt.is_local_model(engine):
        # A specific model key is an explicit user pick and wins over the
        # resident-following heuristic; the bare "local" alias keeps following
        # the active/resident model to avoid multi-GB load/unload cycles.
        key = engine if engine != "local" else resolve_local_key(local_model_key)
        # complete() would download multi-GB weights if the model is absent;
        # never let a summary request trigger that silently.
        if not local_rt.is_downloaded(key):
            raise RuntimeError(f"local map model {key!r} is not downloaded")
        out = local_rt.complete(key, system, user, max_tokens=max_tokens)
        if not out:
            raise RuntimeError("local one-shot completion returned empty")
        return out

    # Everything below reaches Amazon Bedrock (or mantle): never in Local mode.
    from server.infrastructure.cloud_guard import require_cloud

    require_cloud(feature)

    # OpenAI-on-Bedrock cloud key: run the one-shot through the Responses API
    # on mantle with the same model, so provider-aware callers (compaction)
    # keep a GPT session entirely on its own model. Failures raise, matching
    # this function's contract — callers own their fallbacks.
    from server.openai_bedrock.runtime import is_openai_model

    if is_openai_model(cloud_model_key):
        return _openai_one_shot(cloud_model_key, system, user, max_tokens, source, session_id)

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
    from server.costs.calls import invoke_claude

    payload = invoke_claude(
        _get_bedrock_client(), model_id=model_id, body=body, source=source, session_id=session_id
    )
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    if not text:
        raise RuntimeError("cloud one-shot completion returned no text")
    return text


def _openai_one_shot(
    model_key: str, system: str, user: str, max_tokens: int, source: str, session_id: str
) -> str:
    """One blocking Responses-API completion on bedrock-mantle, logged. Raises
    on failure or an empty result (one_shot's contract)."""
    from server.chat.infra import _get_chat_models
    from server.costs.calls import record_responses
    from server.openai_bedrock.runtime import build_sync_client, region_for

    model_id = _get_chat_models().get(model_key)
    if not model_id:
        raise RuntimeError(f"openai one-shot: model key {model_key!r} not configured")
    client = build_sync_client(region_for(model_key))
    request = {
        "model": model_id,
        "instructions": system,
        "input": user,
        "max_output_tokens": max(16, max_tokens),
        "reasoning": {"effort": "low"},
        "store": False,
    }
    resp = client.responses.create(**request)
    record_responses(
        resp, model_key=model_key, request=request, source=source, session_id=session_id
    )
    out = (getattr(resp, "output_text", "") or "").strip()
    if not out:
        raise RuntimeError("openai one-shot completion returned empty")
    return out
