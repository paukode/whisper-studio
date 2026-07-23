"""Session memory — per-session structured summaries.

Maintains a markdown file per session with fixed sections:
Goals, Decisions, Context, Blockers. Updated incrementally
by a background agent after token/tool-call thresholds.
"""

import asyncio
import logging
import os

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

SESSION_MEMORY_DIR = os.path.join(data_root(), "session_memory")

# Thresholds for triggering session memory updates
TOKEN_THRESHOLD_CHARS = 200_000  # ~50k tokens
TOOL_CALL_THRESHOLD = 10
# On-device sessions update far more often (and without needing tool calls):
# the summary runs on the local model itself, so we keep it lean and frequent
# rather than waiting for the large cloud thresholds.
LOCAL_TOKEN_THRESHOLD_CHARS = 6_000  # ~1.5k tokens (every few turns)
MAX_SECTION_CHARS = 2_000  # ~500 words per section
MAX_TOTAL_CHARS = 12_000  # ~3k tokens total

# Track state per session
_session_state: dict[str, dict] = {}  # session_id -> {chars_at_last_update, tool_calls_since}

TEMPLATE = """\
## Goals
(No goals recorded yet)

## Decisions
(No decisions recorded yet)

## Context
(No context recorded yet)

## Blockers
(No blockers recorded yet)
"""


def drop_session(session_id: str) -> None:
    """Forget a session's update-cadence state AND remove its on-disk summary
    file. Called on session delete so neither the in-memory dict grows without
    bound over a long-lived server nor the summary file outlives the session
    it describes. Best-effort: a missing file is fine, and any other OS error
    is logged but never raised (session delete must not fail on cleanup)."""
    _session_state.pop(session_id, None)
    try:
        os.remove(get_session_memory_path(session_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("Failed to remove session memory file for %s: %s", session_id, e)


def get_session_memory_path(session_id: str) -> str:
    os.makedirs(SESSION_MEMORY_DIR, exist_ok=True)
    return os.path.join(SESSION_MEMORY_DIR, f"{session_id}.md")


def load_session_memory(session_id: str) -> str | None:
    """Read session memory file. Returns content or None."""
    path = get_session_memory_path(session_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def get_session_memory_context(session_id: str) -> str:
    """Format session memory for prompt injection. Returns empty string if none."""
    content = load_session_memory(session_id)
    if not content:
        return ""
    return f"<session-memory>\n{content}\n</session-memory>"


def _estimate_chars(messages: list[dict]) -> int:
    """Estimate total character count of messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(block.get("text", ""))
                    total += len(block.get("content", ""))
    return total


def _count_tool_calls_since(messages: list[dict], since_index: int) -> int:
    """Count tool_use blocks since a message index."""
    count = 0
    for msg in messages[since_index:]:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    count += 1
    return count


async def maybe_update_session_memory(
    *,
    messages: list[dict],
    session_id: str,
    model_id: str,
) -> None:
    """Post-query hook for session memory. Background task.

    Guards:
    1. Feature flag
    2. Token threshold (200k chars)
    3. Tool call threshold (10 tool calls since last update)
    """
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("session_memory"):
            return

        from server.local.runtime import is_local_model_id

        is_local = is_local_model_id(model_id)

        state = _session_state.setdefault(
            session_id,
            {
                "chars_at_last_update": 0,
                "last_update_index": 0,
            },
        )

        total_chars = _estimate_chars(messages)
        chars_since = total_chars - state["chars_at_last_update"]
        char_threshold = LOCAL_TOKEN_THRESHOLD_CHARS if is_local else TOKEN_THRESHOLD_CHARS
        if chars_since < char_threshold:
            return

        # Cloud sessions also wait for tool activity; local chats often have no
        # tools, so the char threshold alone gates them.
        if not is_local:
            tool_calls = _count_tool_calls_since(messages, state["last_update_index"])
            if tool_calls < TOOL_CALL_THRESHOLD:
                return

        await _run_session_update(messages, session_id, model_id)

        state["chars_at_last_update"] = total_chars
        state["last_update_index"] = len(messages)

    except Exception as e:
        log.error("Session memory update failed: %s", e, exc_info=True)


def _build_excerpt(messages: list[dict]) -> str:
    """Recent-conversation excerpt for the summary prompt (last 30 turns,
    each text capped). Shared by the cloud and on-device updaters."""
    parts = []
    for msg in messages[-30:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(texts)
        if content:
            parts.append(f"{role}: {content[:300]}")
    return "\n".join(parts)


def _update_task(existing: str, excerpt: str) -> str:
    return (
        f"Update the session memory based on the recent conversation.\n\n"
        f"## Current Session Memory\n{existing}\n\n"
        f"## Recent Conversation\n{excerpt}\n\n"
        f"Write the updated session memory using the same four sections "
        f"(## Goals, ## Decisions, ## Context, ## Blockers). Keep each section "
        f"under {MAX_SECTION_CHARS} characters; total under {MAX_TOTAL_CHARS} "
        f"characters. Output only the markdown, no preamble."
    )


async def _run_session_update(
    messages: list[dict],
    session_id: str,
    model_id: str,
) -> None:
    """Run the session memory update. On-device (local) turns summarise via the
    local model itself (fully offline); cloud turns use the memory_extractor
    agent. Both write the same structured markdown file."""
    from server.local.runtime import is_local_model_id

    existing = load_session_memory(session_id) or TEMPLATE
    excerpt = _build_excerpt(messages)

    if is_local_model_id(model_id):
        content = await _run_local_session_update(existing, excerpt, model_id)
    else:
        content = await _run_agent_session_update(existing, excerpt, session_id)

    if not content:
        return
    if len(content) > MAX_TOTAL_CHARS:
        content = content[:MAX_TOTAL_CHARS]
    path = get_session_memory_path(session_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("Session memory updated for %s", session_id)
    except OSError as e:
        log.warning("Failed to write session memory: %s", e)


async def _run_agent_session_update(existing: str, excerpt: str, session_id: str) -> str | None:
    """Cloud path: summarise via the memory_extractor agent."""
    from server.agents.runtime import run_agent
    from server.memory.prompts import SESSION_SUMMARY_PROMPT

    result = await run_agent(
        _update_task(existing, excerpt),
        agent_type="memory_extractor",
        session_id=session_id,
        context=SESSION_SUMMARY_PROMPT,
        depth=1,
    )
    if result.status == "completed" and result.output:
        return result.output
    return None


async def _run_local_session_update(existing: str, excerpt: str, model_id: str) -> str | None:
    """On-device path: summarise on the local model, keeping session memory
    fully offline for local turns. Runs on the model's executor thread; degrades
    to None (no update) on any failure rather than disrupting the chat."""

    from server.local import runtime as local_llm
    from server.memory.prompts import SESSION_SUMMARY_PROMPT

    key = local_llm.key_for_id(model_id)
    if not key:
        return None

    user = _update_task(existing, excerpt)
    loop = asyncio.get_event_loop()

    def _gen() -> str:
        convo = local_llm.to_chat_messages(
            SESSION_SUMMARY_PROMPT, [{"role": "user", "content": user}]
        )
        return local_llm.generate_round(key, convo, [], max_tokens=1024)

    try:
        text = (await loop.run_in_executor(local_llm.executor, _gen)) or ""
    except Exception as e:  # never let a background summary disrupt anything
        log.warning("Local session summary failed: %s", e)
        return None

    from server.local.tools import strip_tool_markers

    text = strip_tool_markers(text).strip()
    return text or None
