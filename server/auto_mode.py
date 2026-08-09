"""
Auto mode classifier rules for Whisper Studio.

Config-driven allow / soft_deny / environment rules evaluated by a Claude Haiku
classifier before each tool call. The classifier runs whenever the ``auto``
permission mode is selected: tools that clearly match an allow rule are
auto-approved; anything else (soft_deny matches, uncertainty) asks the user.
Rules live in the ``auto_mode_allow`` / ``auto_mode_soft_deny`` /
``auto_mode_environment`` config keys, edited via ``config_set`` in chat.

The classifier also receives a small, bounded slice of the turn's recent
tool-call history (see ``_build_turn_context``) so it can catch a multi-step
pattern split across individually-innocuous steps (e.g. reading a credentials
file, then POSTing its contents somewhere) instead of judging one call
totally in isolation. The rolling denial counter that trips a circuit
breaker on repeated "confirm" verdicts lives in
``server.security.permissions`` (record_classifier_verdict /
is_auto_mode_tripped), next to the mode-resolution logic it overrides.
"""

import asyncio
import json
import logging

log = logging.getLogger("whisper-studio")

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

You may also be shown a few recent steps from earlier in this turn. Individually
innocuous calls can add up to a risky pattern (e.g. reading a credentials file,
then posting its contents somewhere) — weigh that sequence, not just the call
in isolation.

Respond with ONLY a JSON object: {"decision": "allow" | "confirm", "reason": "..."}
Keep the reason under 15 words.
Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen.\
"""

# ── Bounded turn context ─────────────────────────────────────────────────────
# The classifier must stay a fast, cheap call — these caps keep the added
# context small (last 3 messages, ~900 chars total) instead of turning this
# into a full-transcript review. 3 messages is enough to see a short chain
# like "read X" -> "now writing/POSTing Y" without dragging in the whole turn.
_MAX_CONTEXT_MESSAGES = 3
_MAX_CONTEXT_CHARS_PER_MSG = 300
_MAX_CONTEXT_CHARS_TOTAL = 900


def _summarize_message(msg: dict) -> str:
    """Render one assistant message's content as a short, readable line.

    Bedrock-shaped assistant messages carry a list of content blocks
    (text/tool_use/thinking) rather than a plain string; a raw ``str(list)``
    repr is unreadable noise to the classifier. Pull out the tool name +
    input and any text so a prior "called ws_read_file(path=...)" step is
    actually legible.
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:_MAX_CONTEXT_CHARS_PER_MSG]
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name", "?")
                inp = json.dumps(block.get("input", {}), default=str)[:150]
                parts.append(f"called {name}({inp})")
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    parts.append(text[:150])
        return "; ".join(parts)[:_MAX_CONTEXT_CHARS_PER_MSG]
    return str(content)[:_MAX_CONTEXT_CHARS_PER_MSG]


def _build_turn_context(recent_messages: list[dict] | None) -> str:
    """Bounded slice of the turn's recent tool-call history (see module
    docstring). Returns "" when there's nothing to show."""
    if not recent_messages:
        return ""
    snippets = []
    total = 0
    for msg in reversed(recent_messages[-_MAX_CONTEXT_MESSAGES:]):
        text = _summarize_message(msg)
        if not text:
            continue
        if total + len(text) > _MAX_CONTEXT_CHARS_TOTAL:
            break
        snippets.insert(0, text)
        total += len(text)
    if not snippets:
        return ""
    return (
        "Recent steps this turn (oldest first):\n" + "\n".join(f"- {s}" for s in snippets) + "\n\n"
    )


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
    recent_messages: list[dict] | None = None,
) -> dict:
    """
    Classify a tool call as 'allow' or 'confirm'.
    Returns {"decision": "allow"|"confirm", "reason": str}.
    Falls back to 'confirm' on any error. Always classifies with the cheap
    Haiku-tier model from the catalog, never the session model.

    ``recent_messages`` is a bounded slice of this turn's history so far
    (see _build_turn_context) — it lets the classifier catch a multi-step
    pattern instead of judging this one call totally in isolation.
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

    context = _build_turn_context(recent_messages)
    user_msg = (
        f"{context}"
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
