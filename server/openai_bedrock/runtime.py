"""Provider runtime for OpenAI models on Bedrock: model detection, region +
bearer-token auth, client construction, and request mapping.

GPT-5.x (5.4 / 5.5 / 5.6) on Amazon Bedrock speak the OpenAI Responses API on
the bedrock-mantle endpoint. We mint a bearer token from the caller's AWS
credentials (aws-bedrock-token-generator), cache it per region (tokens are valid
for min(12h, session) and are NOT refreshable in place, so we re-mint well
before expiry), and point the openai SDK at the mantle base URL.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")

# OpenAI models use the `/openai/v1` path on bedrock-mantle (other mantle models
# use plain `/v1` — using the wrong one 404s).
_BASE_URL = "https://bedrock-mantle.{region}.api.aws/openai/v1"

# Re-mint tokens every 45 min, comfortably under the 12h cap (and under most STS
# session lifetimes). Keyed by region because a token is region-locked.
_TOKEN_TTL_S = 45 * 60
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()

# Tools dropped from the pool for GPT-5.x: it follows instructions literally and
# turns "think for N seconds" into an actual `sleep` call. Excluded so the model
# reasons instead of stalling on a wait tool.
EXCLUDED_TOOLS = {"sleep"}

# Appended to the (Claude-tuned) system prompt for GPT-5.x turns to curb its
# literal interpretation of timing phrases.
GPT_INSTRUCTIONS_SUFFIX = (
    '\n\nInterpret timing phrases like "think for N seconds" as "reason '
    'briefly," not as a literal wait. Never call a sleep or wait tool to '
    "satisfy them."
)

# App effort label -> OpenAI Responses reasoning effort. The GPT-5.5/5.4 ladder
# tops at "xhigh", so the app's "extra"/"max"/"ultracode" collapse there.
# GPT-5.6 adds a "max" tier above "xhigh" (verified live 2026-07-15; 5.5 rejects
# it with unsupported_value), so the top labels are remapped per model — see
# reasoning_effort_for.
_EFFORT_MAP = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "xhigh",
    "ultracode": "xhigh",
}

# Labels that ride the model's top reasoning tier: "max" where the ladder has
# it (GPT-5.6+), else the _EFFORT_MAP default of "xhigh".
_TOP_TIER_LABELS = {"max", "ultracode"}

_GPT_VERSION_RE = re.compile(r"openai\.gpt-(\d+)\.(\d+)")


def _model_meta(model_key: str) -> dict:
    from server.chat.infra import _get_chat_model_meta

    return _get_chat_model_meta().get(model_key, {})


def is_openai_model(model_key: str) -> bool:
    """True iff this model routes through the OpenAI-on-Bedrock provider."""
    return _model_meta(model_key).get("provider") == "openai_bedrock"


def region_for(model_key: str) -> str:
    """Region for an OpenAI-on-Bedrock model, resolved entirely from config —
    no hardcoded default. Same as the Anthropic path: the account-wide
    ``bedrock_region`` drives it. An optional per-model ``openai_region`` in
    config wins when set (escape hatch for a model that isn't in the account's
    default region). ``load_config`` guarantees ``bedrock_region`` is a
    non-empty, stripped region string, so there is nothing to fall back to."""
    override = (_model_meta(model_key).get("openai_region") or "").strip()
    return override or load_config()["bedrock_region"]


def verbosity_for(model_key: str, body: dict | None = None) -> str:
    """GPT-5.x verbosity (text.verbosity). Per-request override wins, then the
    model's config default, else 'medium'."""
    if body and body.get("verbosity") in ("low", "medium", "high"):
        return body["verbosity"]
    v = _model_meta(model_key).get("verbosity")
    return v if v in ("low", "medium", "high") else "medium"


def _supports_max_effort(model_key: str) -> bool:
    """True iff the model's reasoning ladder includes "max" (GPT-5.6 and up)."""
    m = _GPT_VERSION_RE.match(_model_meta(model_key).get("id", ""))
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (5, 6)


def reasoning_effort_for(model_key: str, effort_label: str | None) -> str:
    """Map the app's effort level to this model's reasoning.effort value."""
    if not effort_label:
        return "medium"
    if effort_label in _TOP_TIER_LABELS and _supports_max_effort(model_key):
        return "max"
    return _EFFORT_MAP.get(effort_label, "medium")


def _get_bearer_token(region: str) -> str:
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(region)
        if cached is not None and (now - cached[1]) < _TOKEN_TTL_S:
            return cached[0]
    # Mint outside the lock (network/crypto). A concurrent double-mint is
    # harmless — last writer wins and both tokens are valid.
    from aws_bedrock_token_generator import provide_token

    token = provide_token(region=region)
    with _token_lock:
        _token_cache[region] = (token, now)
    return token


def reset_token_cache() -> None:
    """Drop cached tokens (test hook / force a re-mint after an auth failure)."""
    with _token_lock:
        _token_cache.clear()


def build_client(region: str):
    """Fresh AsyncOpenAI client for the bedrock-mantle OpenAI endpoint with a
    current bearer token. Cheap to construct; we make a new one per turn so a
    refreshed token is always picked up."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=_get_bearer_token(region),
        base_url=_BASE_URL.format(region=region),
        # Generous backstop so a genuinely long reasoning gap doesn't trip a
        # spurious client timeout; the stream adapter early-releases and
        # heartbeats well before this fires.
        timeout=120.0,
        max_retries=1,
    )


def translate_tools(tool_pool: list[dict]) -> list[dict]:
    """Anthropic-style tool defs {name, description, input_schema} -> OpenAI
    Responses FLAT function tools {type, name, description, parameters}.

    Note the FLAT shape (no nested "function" wrapper) — that is the Responses
    API form, unlike Chat Completions."""
    out: list[dict] = []
    for t in tool_pool:
        out.append(
            {
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return out


def _content_parts(blocks: list, role: str) -> list[dict]:
    """Anthropic-style content blocks -> Responses content parts, preserving
    images so GPT-5.x (a vision model) sees the pixels, not just OCR text.

    Responses uses ``input_text``/``input_image`` for user/system/developer and
    ``output_text`` for assistant. Anthropic image blocks
    ({type:image, source:{type:base64, media_type, data}}) become an
    ``input_image`` data URL."""
    text_type = "output_text" if role == "assistant" else "input_text"
    parts: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            parts.append({"type": text_type, "text": str(b)})
            continue
        if isinstance(b.get("text"), str):
            parts.append({"type": text_type, "text": b["text"]})
        elif b.get("type") == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64" and src.get("data") and text_type == "input_text":
                media = src.get("media_type", "image/png")
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{media};base64,{src['data']}",
                    }
                )
        elif b.get("type") == "tool_result":
            parts.append({"type": text_type, "text": str(b.get("content", ""))})
    if not parts:
        parts.append({"type": text_type, "text": ""})
    return parts


def to_responses_input(messages: list[dict]) -> list[dict]:
    """App messages [{role, content}] -> Responses ``input`` items. Roles map 1:1
    (user/assistant). String content passes through; list content (multimodal:
    text + image attachments) maps to typed parts so images reach the model. The
    system prompt is sent separately as ``instructions``, not as an input item."""
    items: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system", "developer"):
            role = "user"
        content = m.get("content", "")
        if isinstance(content, list):
            items.append({"role": role, "content": _content_parts(content, role)})
        else:
            items.append({"role": role, "content": content})
    return items


def tool_result_input_items(tool_use_id: str, content) -> list[dict]:
    """Render ONE tool result as Responses ``input`` item(s).

    A plain-text result -> a single ``function_call_output``. An image result
    (the list of Anthropic content blocks a screenshot/preview tool returns via
    ``process_tool_results``) -> the caption as the ``function_call_output``
    output, FOLLOWED by a ``user`` message carrying the image as an
    ``input_image`` part. That second message is how a tool-produced image
    reaches GPT-5.x (a vision model): ``function_call_output.output`` is
    text-only on this endpoint, so ``str(content)`` would ship a stringified
    base64 blob the model can't see. Mirrors ``_content_parts`` for user
    attachments."""
    if not isinstance(content, list):
        return [
            {
                "type": "function_call_output",
                "call_id": tool_use_id,
                "output": str(content),
            }
        ]

    texts: list[str] = []
    images: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            texts.append(str(b))
        elif isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif b.get("type") == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                media = src.get("media_type", "image/png")
                images.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{media};base64,{src['data']}",
                    }
                )

    caption = "\n".join(t for t in texts if t)
    items: list[dict] = [
        {
            "type": "function_call_output",
            "call_id": tool_use_id,
            # Responses rejects an empty output; the pixels ride in the follow-up
            # user message, so leave a pointer when the tool gave no text.
            "output": caption or "[image result — see the following message]",
        }
    ]
    if images:
        items.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Tool image output:"}, *images],
            }
        )
    return items
