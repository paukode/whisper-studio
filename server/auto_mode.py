"""
Auto mode classifier rules for Whisper Studio.

Config-driven allow / soft_deny / environment rules evaluated by a Claude Haiku
classifier before each tool call. When enabled, tools that clearly match an
allow rule are auto-approved; tools matching a soft_deny rule prompt the user.

Classifier evaluates each tool call against configured rules before execution.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/auto-mode", tags=["auto-mode"])

# ── Default rules ──────────────────────────────────────────────────────────────

DEFAULT_ALLOW_RULES = [
    "Read files, list directories, search for patterns in code",
    "Fetch web pages and perform web searches",
    "Run read-only shell commands (e.g. git status, git log, ls, cat, grep, find)",
    "Query databases in read-only mode",
    "List MCP resources and call read-only MCP tools",
    "Create notebook cells without executing them",
    "Retrieve task or cron status",
]

DEFAULT_SOFT_DENY_RULES = [
    "Delete or permanently remove files or directories",
    "Run commands that modify system state (package installs, service restarts)",
    "Write or overwrite files outside the current workspace",
    "Execute arbitrary shell commands that are not clearly read-only",
    "Send emails, post to external services, or trigger webhooks",
    "Modify CI/CD pipelines or deployment configs",
]

DEFAULT_ENVIRONMENT_RULES: list[str] = []

# ── System prompts ──────────────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM_PROMPT = """\
You are a security classifier for an AI coding assistant called Whisper Studio.

Your job is to decide whether a specific tool call should be AUTO-APPROVED or \
requires USER CONFIRMATION based on the rules below.

Rules are in three categories:
- **allow**: Tool calls matching these rules should be auto-approved.
- **soft_deny**: Tool calls matching these rules require user confirmation.
- **environment**: Context about the user's setup that helps you make decisions.

Respond with ONLY a JSON object: {"decision": "allow" | "confirm", "reason": "..."}
Keep the reason under 15 words.
Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.\
"""

_CRITIQUE_SYSTEM_PROMPT = """\
You are an expert reviewer of auto mode classifier rules for an AI coding assistant.

The assistant has an "auto mode" that uses an AI classifier to decide whether tool \
calls should be auto-approved or require user confirmation. Users can write custom \
rules in three categories:

- **allow**: Actions the classifier should auto-approve
- **soft_deny**: Actions the classifier should block (require user confirmation)
- **environment**: Context about the user's setup that helps the classifier make decisions

Your job is to critique the user's custom rules for clarity, completeness, \
and potential issues. The classifier is an LLM that reads these rules as \
part of its system prompt.

For each rule, evaluate:
1. **Clarity**: Is the rule unambiguous? Could the classifier misinterpret it?
2. **Completeness**: Are there gaps or edge cases the rule doesn't cover?
3. **Conflicts**: Do any of the rules conflict with each other?
4. **Actionability**: Is the rule specific enough for the classifier to act on?

Be concise and constructive. Only comment on rules that could be improved. \
If all rules look good, say so.\
"""


def get_effective_rules(config: dict) -> dict:
    """Return effective allow/soft_deny/environment rules (user overrides defaults per-section)."""
    user_allow = config.get("auto_mode_allow", [])
    user_soft_deny = config.get("auto_mode_soft_deny", [])
    user_env = config.get("auto_mode_environment", [])
    return {
        "allow": user_allow if user_allow else DEFAULT_ALLOW_RULES,
        "soft_deny": user_soft_deny if user_soft_deny else DEFAULT_SOFT_DENY_RULES,
        "environment": user_env if user_env else DEFAULT_ENVIRONMENT_RULES,
    }


def _build_classifier_prompt(rules: dict) -> str:
    allow_lines = "\n".join(f"- {r}" for r in rules["allow"])
    deny_lines = "\n".join(f"- {r}" for r in rules["soft_deny"])
    env_lines = (
        "\n".join(f"- {r}" for r in rules["environment"]) if rules["environment"] else "(none)"
    )
    return (
        f"{_CLASSIFIER_SYSTEM_PROMPT}\n\n"
        f"## allow rules\n{allow_lines}\n\n"
        f"## soft_deny rules\n{deny_lines}\n\n"
        f"## environment\n{env_lines}"
    )


async def classify_tool_call(
    tool_name: str,
    tool_input: dict,
    config: dict,
    model_id: str,
) -> dict:
    """
    Classify a tool call as 'allow' or 'confirm'.
    Returns {"decision": "allow"|"confirm", "reason": str}.
    Falls back to 'confirm' on any error.
    """
    import boto3

    from server.infrastructure.config import load_config as _load_config

    cfg = config or _load_config()
    rules = get_effective_rules(cfg)
    system = _build_classifier_prompt(rules)
    region = cfg.get("bedrock_region", "us-east-1")
    from server.infrastructure.config import DEFAULTS

    chat_models = cfg.get("chat_models", DEFAULTS["chat_models"])
    haiku = (
        chat_models.get("haiku") or chat_models.get("sonnet") or next(iter(chat_models.values()))
    )

    user_msg = (
        f"Tool: {tool_name}\n"
        f"Input: {json.dumps(tool_input, default=str)[:500]}\n\n"
        "Should this be auto-approved or require user confirmation?"
    )

    try:
        bedrock = boto3.client("bedrock-runtime", region_name=region)
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            }
        )

        def _invoke():
            resp = bedrock.invoke_model(modelId=haiku, body=body)
            return json.loads(resp["body"].read())["content"][0]["text"].strip()

        text = await asyncio.get_running_loop().run_in_executor(None, _invoke)
        # Parse JSON response
        import re

        m = re.search(r"\{[^}]+\}", text)
        if m:
            result = json.loads(m.group())
            decision = result.get("decision", "confirm")
            if decision not in ("allow", "confirm"):
                decision = "confirm"
            return {"decision": decision, "reason": result.get("reason", "")}
    except Exception as e:
        log.warning("Auto mode classifier failed for %s: %s", tool_name, e)
    return {"decision": "confirm", "reason": "classifier unavailable"}


# ── API routes ─────────────────────────────────────────────────────────────────


@router.get("/rules")
async def get_rules():
    from server.infrastructure.config import load_config

    config = load_config()
    return {
        "effective": get_effective_rules(config),
        "defaults": {
            "allow": DEFAULT_ALLOW_RULES,
            "soft_deny": DEFAULT_SOFT_DENY_RULES,
            "environment": DEFAULT_ENVIRONMENT_RULES,
        },
        "enabled": config.get("auto_mode_enabled", False),
    }


@router.post("/critique")
async def critique_rules(request: Request):
    import boto3

    from server.infrastructure.config import load_config

    config = load_config()
    body = await request.json()
    from server.infrastructure.config import DEFAULTS

    chat_models = config.get("chat_models", DEFAULTS["chat_models"])
    model_id = (
        body.get("model")
        or chat_models.get("haiku")
        or chat_models.get("sonnet")
        or next(iter(chat_models.values()))
    )

    user_allow = config.get("auto_mode_allow", [])
    user_soft_deny = config.get("auto_mode_soft_deny", [])
    user_env = config.get("auto_mode_environment", [])

    has_custom = bool(user_allow or user_soft_deny or user_env)
    if not has_custom:
        return {
            "critique": "No custom rules found. Add rules under auto_mode_allow, auto_mode_soft_deny, or auto_mode_environment in config."
        }

    def _fmt(section, user_rules, defaults):
        if not user_rules:
            return ""
        custom = "\n".join(f"- {r}" for r in user_rules)
        defs = "\n".join(f"- {r}" for r in defaults)
        return f"## {section} (custom, replacing defaults)\nCustom:\n{custom}\n\nDefaults being replaced:\n{defs}\n\n"

    rules_text = (
        _fmt("allow", user_allow, DEFAULT_ALLOW_RULES)
        + _fmt("soft_deny", user_soft_deny, DEFAULT_SOFT_DENY_RULES)
        + _fmt("environment", user_env, DEFAULT_ENVIRONMENT_RULES)
    )

    classifier_prompt = _build_classifier_prompt(get_effective_rules(config))

    try:
        region = config.get("bedrock_region", "us-east-1")
        bedrock = boto3.client("bedrock-runtime", region_name=region)
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "system": _CRITIQUE_SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Here is the full classifier system prompt:\n\n"
                            f"<classifier_system_prompt>\n{classifier_prompt}\n</classifier_system_prompt>\n\n"
                            f"Here are the user's custom rules:\n\n{rules_text}\nPlease critique these custom rules."
                        ),
                    }
                ],
            }
        )

        def _invoke():
            resp = bedrock.invoke_model(modelId=model_id, body=body)
            return json.loads(resp["body"].read())["content"][0]["text"].strip()

        text = await asyncio.get_running_loop().run_in_executor(None, _invoke)
        return {"critique": text}
    except Exception as e:
        return {"error": str(e)}
