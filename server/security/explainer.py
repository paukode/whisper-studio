"""
Permission explainer for Whisper Studio.

Makes a side call to Claude Haiku to assess the risk level of a pending
permission approval and return a short explanation for the user.

Returns: {"riskLevel": "LOW"|"MEDIUM"|"HIGH", "explanation": str, "reasoning": str, "risk": str}
Returns None on any error or if disabled in config — always non-blocking.
"""

import asyncio
import json
import logging
import re

log = logging.getLogger("whisper-studio")

_EXPLAINER_TIMEOUT = 3.0  # seconds — hard limit so approval dialog is never delayed

_SYSTEM_PROMPT = """\
You are a security risk assessor for an AI coding assistant called Whisper Studio.

A tool call is about to be executed and the user is being asked to approve it.
Your job is to assess the risk of allowing this action and explain it clearly.

Respond with ONLY a JSON object with these exact fields:
{
  "riskLevel": "LOW" | "MEDIUM" | "HIGH",
  "explanation": "<1-2 sentences describing what the action does>",
  "reasoning": "<starting with 'I need to...', explains why the assistant wants to do this>",
  "risk": "<under 15 words summarising the risk>"
}

Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.

Risk levels:
- LOW: reversible, version-controlled, or read-only actions
- MEDIUM: writes to important files, installs packages, config changes
- HIGH: deletes, irreversible changes, external service calls, credentials
"""


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
        f"Input: {json.dumps(tool_input, default=str)[:500]}\n\n"
        "Assess the risk of approving this action."
    )


async def explain_permission(
    tool_name: str,
    tool_input: dict,
    recent_messages: list[dict],
    config: dict,
    model_id: str,
) -> dict | None:
    """
    Returns risk explanation dict or None (on error / disabled / timeout).
    Never raises.
    """
    if not config.get("permission_explainer_enabled", True):
        return None

    try:
        result = await asyncio.wait_for(
            _call_explainer(tool_name, tool_input, recent_messages, config, model_id),
            timeout=_EXPLAINER_TIMEOUT,
        )
        return result
    except asyncio.TimeoutError:
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
    model_id: str,
) -> dict | None:
    import boto3

    from server.infrastructure.config import DEFAULTS

    region = config.get("bedrock_region", "us-east-1")
    chat_models = config.get("chat_models", DEFAULTS["chat_models"])
    haiku = (
        chat_models.get("haiku") or chat_models.get("sonnet") or next(iter(chat_models.values()))
    )

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
        resp = bedrock.invoke_model(modelId=haiku, body=body)
        return json.loads(resp["body"].read())["content"][0]["text"].strip()

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
        "explanation": str(parsed.get("explanation", ""))[:300],
        "reasoning": str(parsed.get("reasoning", ""))[:200],
        "risk": str(parsed.get("risk", ""))[:100],
    }
