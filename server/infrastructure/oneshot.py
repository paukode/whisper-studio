"""Mode-aware blocking one-shot completion, with no model substitution on failure."""

import json
from collections.abc import Callable

_LOCAL_KEY = "local_gemma"

# The hybrid "One-shot writer: Off" value: no model runs the map step.
OFF = "none"


def resolve_local_key(local_model_key: str | None) -> str:
    """Resolve the explicit local alias, following active/resident/downloaded keys.

    one_shot still checks download availability before calling complete.
    """
    from server.local import runtime as local_rt

    if local_model_key:
        if not local_rt.is_local_model(local_model_key):
            raise RuntimeError("requested local one-shot model is not configured")
        return local_model_key
    resident = local_rt.loaded_key()
    if resident and local_rt.is_local_model(resident):
        return resident
    for k in local_rt.LOCAL_MODELS:
        if local_rt.is_downloaded(k):
            return k
    return _LOCAL_KEY


def resolve_map_engine(config: dict | None = None) -> str:
    """The one-shot writer for the active mode: "haiku", "local", OFF, or an
    installed on-device model key. Any other value (for example an on-device
    model that was removed) raises instead of sending the call to the cloud."""
    from server.infrastructure.model_mode import resolve_backend
    from server.local import registry as local_registry

    engine = resolve_backend("index_llm", config)
    if engine in ("haiku", "local", OFF) or engine in local_registry.local_models():
        return engine
    raise RuntimeError(
        f"The one-shot writer (Settings > Model mode) is set to {engine!r}, which is not "
        "an installed on-device model. Choose another one-shot writer."
    )


def _is_claude_id(model_id: str) -> bool:
    """Claude InvokeModel ids only, excluding the data-retention gated Fable."""
    if not isinstance(model_id, str):
        return False
    mid = model_id.lower()
    if not mid or mid.startswith("local:") or mid.startswith("openai."):
        return False
    if "fable" in mid:
        return False
    return "claude" in mid


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
    get_bedrock_client: Callable[[], object] | None = None,
) -> str:
    """Run exactly one selected completion and return nonblank, complete text.

    Known cloud keys route to Claude or OpenAI; unknown/unsupported keys fail
    before client construction. Local mode refuses cloud keys, never replaces
    them with local ones. The explicit local alias retains resident selection.
    Local weights must already be downloaded. Cloud usage is recorded before
    incomplete or filtered results are rejected. source/session/max_tokens
    retain their existing routing and cost-accounting semantics.
    """
    engine = engine or resolve_map_engine()
    if engine == OFF:
        raise RuntimeError(
            "the one-shot writer is Off (Settings > Model mode), so no model runs this call"
        )

    from server.local import runtime as local_rt

    if engine == "local" or local_rt.is_local_model(engine):
        key = engine if engine != "local" else resolve_local_key(local_model_key)
        if not local_rt.is_downloaded(key):
            raise RuntimeError(f"local map model {key!r} is not downloaded")
        out = _local_one_shot(key, system, user, max_tokens=max_tokens)
        if not isinstance(out, str) or not out.strip():
            raise RuntimeError("local one-shot completion returned empty")
        return out
    if engine not in ("cloud", "haiku"):
        raise RuntimeError("requested one-shot engine is not configured")

    from server.infrastructure.cloud_guard import require_cloud

    require_cloud(feature)

    from server.chat.infra import _get_bedrock_client, _get_chat_models
    from server.openai_bedrock.runtime import is_openai_model

    models = _get_chat_models()
    model_id = models.get(cloud_model_key)
    if not isinstance(model_id, str) or not model_id:
        raise RuntimeError("requested cloud one-shot model is not configured")
    if is_openai_model(cloud_model_key):
        if not model_id.startswith("openai."):
            raise RuntimeError("unsupported cloud one-shot model")
        return _openai_one_shot(
            cloud_model_key, model_id, system, user, max_tokens, source, session_id
        )
    if not _is_claude_id(model_id):
        raise RuntimeError("unsupported cloud one-shot model")

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    )
    from server.costs.calls import invoke_claude

    client = (get_bedrock_client or _get_bedrock_client)()
    payload = invoke_claude(
        client, model_id=model_id, body=body, source=source, session_id=session_id
    )
    # invoke_claude has already captured usage, including unsuccessful output.
    if payload.get("stop_reason") not in ("end_turn", "stop_sequence"):
        raise RuntimeError("cloud one-shot completion was incomplete or refused")
    if payload.get("amazon-bedrock-guardrailAction") == "INTERVENED":
        raise RuntimeError("cloud one-shot completion was filtered")
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise RuntimeError("cloud one-shot completion returned no text")
    return text


def _local_one_shot(key: str, system: str, user: str, *, max_tokens: int) -> str:
    """Keep local finish metadata until validated, unlike runtime.complete's text API."""
    import httpx

    from server.local import llama_server, serving

    url = serving.ensure_serving(key)
    response = httpx.post(
        f"{url}/v1/chat/completions",
        json={
            "model": serving.wire_model(key),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=serving._ONESHOT_TIMEOUT,
    )
    response.raise_for_status()
    choices = (response.json() or {}).get("choices") or []
    if not choices or choices[0].get("finish_reason") != "stop":
        raise RuntimeError("local one-shot completion was incomplete or refused")
    message = choices[0].get("message") or {}
    if message.get("refusal") or message.get("tool_calls"):
        raise RuntimeError("local one-shot completion was refused or requested tools")
    return llama_server._strip_reasoning_markers(message.get("content") or "")


def _field(value, key, default=None):
    """Read provider SDK objects or their JSON equivalents."""
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _openai_one_shot(
    model_key: str,
    model_id: str,
    system: str,
    user: str,
    max_tokens: int,
    source: str,
    session_id: str,
) -> str:
    """One Responses completion on mantle, logged before result validation."""
    from server.costs.calls import record_responses
    from server.openai_bedrock.runtime import build_sync_client, region_for

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
    if (
        _field(resp, "status") != "completed"
        or _field(resp, "incomplete_details")
        or _field(resp, "error")
    ):
        raise RuntimeError("openai one-shot completion was incomplete or refused")
    for item in _field(resp, "output", []) or []:
        status = _field(item, "status")
        if status not in (None, "completed") or (
            _field(item, "type") == "message" and status != "completed"
        ):
            raise RuntimeError("openai one-shot output was incomplete")
        if any(_field(part, "type") == "refusal" for part in _field(item, "content", []) or []):
            raise RuntimeError("openai one-shot completion was refused")
    out = (_field(resp, "output_text", "") or "").strip()
    if not out:
        raise RuntimeError("openai one-shot completion returned empty")
    return out
