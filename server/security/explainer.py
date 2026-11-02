"""
Permission explainer for Whisper Studio.

Makes a side call to Claude Haiku to assess the risk level of a pending
permission approval and return a short explanation for the user.

Returns: {"riskLevel": "LOW"|"MEDIUM"|"HIGH", "explanation": str, "reasoning": str, "risk": str}
Returns None on any error or if disabled in config, always non-blocking.
"""

import asyncio
import json
import logging
import re

log = logging.getLogger("whisper-studio")

_EXPLAINER_TIMEOUT = 3.0  # seconds, hard limit so approval dialog is never delayed

_SYSTEM_PROMPT = """\
You are a security risk assessor for an AI coding assistant called Whisper Studio.

A tool call is about to be executed and the user is being asked to approve it.
Your job is to assess the risk of allowing this action and explain it clearly.

Respond with ONLY a JSON object with these exact fields:
{
  "riskLevel": "LOW" | "MEDIUM" | "HIGH",
  "explanation": "<1-2 sentences, under 40 words, describing what the action does>",
  "reasoning": "<one sentence, under 30 words, starting with 'I need to...', explains why the assistant wants to do this>",
  "risk": "<under 15 words summarising the risk>"
}

Write complete sentences that fit those limits; longer text is cut.

Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.

Risk levels:
- LOW: reversible, version-controlled, or read-only actions
- MEDIUM: writes to important files, installs packages, config changes
- HIGH: deletes, irreversible changes, external service calls, credentials
"""


# Field limits of the explanation the card shows (characters).
_EXPLANATION_CHARS = 300
_REASONING_CHARS = 200
_RISK_CHARS = 100
# Limits of the tool input sent to the explainer: each string value, then the
# whole input as JSON.
_INPUT_VALUE_CHARS = 400
_INPUT_CHARS = 1200


def clip_words(text: str, limit: int) -> str:
    """``text`` at most ``limit`` characters long, cut at a word boundary
    with an ellipsis, never inside a word. Text that fits is unchanged."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    cut = max(head.rfind(" "), head.rfind("\n"))
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip(" \n,;:-") + "\u2026"


def _clip_input(value):
    """The tool input with every long string value clipped at a word
    boundary, so the JSON the explainer reads stays valid and readable."""
    if isinstance(value, str):
        return clip_words(value, _INPUT_VALUE_CHARS)
    if isinstance(value, dict):
        return {k: _clip_input(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_input(v) for v in value]
    return value


def _input_text(tool_input: dict) -> str:
    text = json.dumps(_clip_input(tool_input), default=str)
    if len(text) <= _INPUT_CHARS:
        return text
    # Still too long (many fields): drop whole fields from the end, so no
    # key or value is cut in half.
    kept: dict = {}
    for key, value in tool_input.items():
        trial = json.dumps(_clip_input({**kept, key: value}), default=str)
        if len(trial) > _INPUT_CHARS:
            break
        kept[key] = value
    kept["..."] = "more fields not shown"
    return json.dumps(_clip_input(kept), default=str)


def _build_user_message(tool_name: str, tool_input: dict, recent_messages: list[dict]) -> str:
    context = ""
    if recent_messages:
        snippets = []
        total = 0
        for msg in reversed(recent_messages[-3:]):
            text = str(msg.get("content", ""))[:400]
            if total + len(text) > 1000:
                break
            snippets.insert(0, text)
            total += len(text)
        if snippets:
            context = "Recent assistant context:\n" + "\n---\n".join(snippets) + "\n\n"
    return (
        f"{context}"
        f"Tool: {tool_name}\n"
        f"Input: {_input_text(tool_input)}\n\n"
        "Assess the risk of approving this action."
    )


async def explain_permission(
    tool_name: str,
    tool_input: dict,
    recent_messages: list[dict],
    config: dict,
    session_id: str = "",
) -> dict | None:
    """
    Returns risk explanation dict or None (on error / disabled / timeout).
    Never raises.
    """
    if not config.get("permission_explainer_enabled", True):
        return None

    try:
        result = await asyncio.wait_for(
            _call_explainer(tool_name, tool_input, recent_messages, config, session_id),
            timeout=_EXPLAINER_TIMEOUT,
        )
        return result
    except TimeoutError:
        log.debug("Permission explainer timed out for %s", tool_name)
        return None
    except Exception as e:
        log.debug("Permission explainer error for %s: %s", tool_name, e)
        return None


async def _call_explainer(
    tool_name: str,
    tool_input: dict,
    recent_messages: list[dict],
    config: dict,
    session_id: str = "",
) -> dict | None:
    import boto3

    region = config.get("bedrock_region", "us-east-1")
    chat_models = config["chat_models"]
    from server.infrastructure.auxiliary import aux_model_id

    haiku = aux_model_id(
        "permission_explainer",
        fallback_id=chat_models.get("sonnet5.5") or next(iter(chat_models.values())),
        models=chat_models,
        config=config,
    )
    if not haiku:
        # No cloud model resolves here (Local mode): no explanation, nothing sent.
        return None

    user_msg = _build_user_message(tool_name, tool_input, recent_messages)

    loop = asyncio.get_event_loop()
    bedrock = boto3.client("bedrock-runtime", region_name=region)

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 200,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        }
    )

    def _invoke():
        # Recorded in the worker, so a call the 3s timeout abandoned is still
        # logged: it was billed all the same.
        from server.costs.calls import invoke_claude

        payload = invoke_claude(
            bedrock, model_id=haiku, body=body, source="explainer", session_id=session_id
        )
        return payload["content"][0]["text"].strip()

    text = await loop.run_in_executor(None, _invoke)

    m = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if not m:
        return None

    parsed = json.loads(m.group())
    risk_level = parsed.get("riskLevel", "").upper()
    if risk_level not in ("LOW", "MEDIUM", "HIGH"):
        return None

    return {
        "riskLevel": risk_level,
        "explanation": clip_words(str(parsed.get("explanation", "")), _EXPLANATION_CHARS),
        "reasoning": clip_words(str(parsed.get("reasoning", "")), _REASONING_CHARS),
        "risk": clip_words(str(parsed.get("risk", "")), _RISK_CHARS),
    }
