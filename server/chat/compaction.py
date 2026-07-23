"""Context compaction: keeps the messages list under Bedrock's 200k-token
input cap during long, tool-heavy conversations.

Three layered strategies, in order of preference:

1. ``microcompact_messages`` — pure local heuristic. Walks the conversation
   and progressively truncates older tool_result blocks. Cheap, no API
   call. Always runs as the pre-pass.

2. Session memory shortcut — if a session has accumulated memory (see
   ``server.memory.session_memory``), we use that as the summary instead
   of calling Claude again. Zero API cost.

3. ``compact_messages_with_claude`` — async Bedrock summarisation as
   the production path. Falls back to ``_compact_messages_simple``
   (deterministic truncation) if the summarisation call fails.

``COMPACT_TRIGGER_CHARS`` / ``MAX_CONTEXT_CHARS`` thresholds are sized so
the chat endpoint compacts pre-emptively (at 650K chars, below Bedrock's
200K-token / ~800K-char input cap) rather than waiting for a Bedrock 400 on
prompt-too-long.
"""

import asyncio
import json
import logging

from .infra import _get_bedrock_client

log = logging.getLogger("whisper-studio")

# Feature 2: Compact when messages exceed this token estimate.
# Bedrock's input cap is 200K tokens (~800K chars at the codebase's
# 4-chars/token estimate). Keep both thresholds
# below that cap so compaction fires proactively instead of every long
# conversation hitting a billed PromptTooLongError. COMPACT_TRIGGER_CHARS is the
# proactive trigger; MAX_CONTEXT_CHARS is the last-resort ceiling the
# simple-truncation fallback trims down to, so it stays at or above the trigger.
MAX_CONTEXT_CHARS = 750_000
COMPACT_TRIGGER_CHARS = 650_000  # Compact pre-emptively at this size


def _content_size(content) -> int:
    """Char size of a message/block ``content`` field.

    Content is either a plain string or a list of blocks, and blocks nest: a
    ``tool_result``'s ``content`` can itself be a list of text/image blocks, and
    an image block carries its (large) base64 payload under ``source.data``.
    Recurse so both are counted, rather than taking ``len()`` of a list — which
    returns the block *count* (a handful), badly under-counting the real size
    and letting an image-heavy history slip past the compaction trigger.
    """
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, str):
            total += len(block)
        elif isinstance(block, dict):
            total += len(block.get("text", ""))
            source = block.get("source")
            if isinstance(source, dict):
                total += len(source.get("data", ""))
            total += _content_size(block.get("content", ""))
    return total


def estimate_message_size(messages: list) -> int:
    total = 0
    for msg in messages:
        total += _content_size(msg.get("content", ""))
        total += 20
    return total


def microcompact_messages(messages: list, keep_recent: int = 6) -> list:
    """Smarter microcompact: age-based selective removal of tool results.

    Strategy:
    - Recent messages (last `keep_recent`): keep intact
    - Older messages: progressively truncate tool_result blocks
      - Oldest third: remove tool results entirely (replace with one-line summary)
      - Middle third: truncate to 500 chars
      - Newer third: truncate to 2000 chars
    """
    total = len(messages)
    if total <= keep_recent:
        return messages

    old_count = total - keep_recent
    tier1_end = old_count // 3  # Oldest: strip tool results
    tier2_end = (old_count * 2) // 3  # Middle: heavy truncation

    out = []
    for i, m in enumerate(messages):
        is_recent = i >= total - keep_recent
        if is_recent or m.get("role") != "user":
            out.append(m)
            continue

        content = m.get("content", "")
        if not isinstance(content, list):
            out.append(m)
            continue

        new_content = []
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                new_content.append(b)
                continue

            result_text = b.get("content", "")
            if not isinstance(result_text, str):
                new_content.append(b)
                continue

            if i < tier1_end:
                # Oldest tier: replace with one-line summary
                summary = result_text[:80].replace("\n", " ")
                new_content.append({**b, "content": f"[{summary}... (removed for compaction)]"})
            elif i < tier2_end:
                # Middle tier: heavy truncation
                if len(result_text) > 500:
                    new_content.append({**b, "content": result_text[:500] + "\n...[truncated]"})
                else:
                    new_content.append(b)
            else:
                # Newer old: moderate truncation
                if len(result_text) > 2000:
                    new_content.append({**b, "content": result_text[:2000] + "\n...[truncated]"})
                else:
                    new_content.append(b)

        out.append({**m, "content": new_content})
    return out


def _is_clean_user_start(msg: dict) -> bool:
    """True if `msg` can legally START a Bedrock message list: a user message
    with no tool_result blocks. A tool_result at the head is invalid once its
    matching assistant tool_use has been summarized/dropped away — Bedrock
    rejects it with a non-retryable ValidationException."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return True


def _safe_recent_split(messages: list, keep_recent: int) -> int:
    """Index at which to split so `messages[split:]` begins on a clean user
    turn, never a dangling tool_result. Starts from the desired keep_recent
    boundary and moves EARLIER (growing the recent window) until the boundary
    is clean. Returns 0 if no clean boundary exists in range — the caller then
    leaves the history intact rather than emit an invalid one."""
    split = max(0, len(messages) - keep_recent)
    while split > 0 and not _is_clean_user_start(messages[split]):
        split -= 1
    return split


def ensure_valid_start(messages: list) -> list:
    """Drop leading messages until the list starts on a clean user turn, so a
    history that was sliced mid tool_use/tool_result pair never begins with an
    orphaned tool_result. Returns the (possibly shorter) list."""
    i = 0
    while i < len(messages) and not _is_clean_user_start(messages[i]):
        i += 1
    return messages[i:]


def _block_ids(content, block_type: str, id_key: str) -> set:
    """tool_use ids (or tool_result tool_use_ids) present in a content list."""
    out: set = set()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == block_type and b.get(id_key):
                out.add(b[id_key])
    return out


def sanitize_tool_pairs(messages: list) -> list:
    """Drop orphaned tool_use / tool_result blocks anywhere in the history.

    Bedrock requires every tool_result block to sit in the message immediately
    after the one carrying its matching tool_use (and every tool_use to be
    answered by a tool_result in the very next message). An interrupted turn — a
    killed stream, a partial assistant turn whose tool_use blocks were stripped,
    a compaction that removed one side of a pair — can persist a tool_result
    whose tool_use is gone (or vice-versa). Bedrock then rejects the ENTIRE
    request non-retryably ("unexpected tool_use_id ... in tool_result blocks"),
    so the session gets wedged: no new message can be sent.

    This pass self-heals such histories at send time. Validity is judged against
    each message's ORIGINAL immediate neighbours; orphaned blocks are dropped. A
    message emptied by the drop keeps its role with a single placeholder text
    block, so message count and user/assistant alternation are untouched (which
    avoids introducing a fresh Bedrock validation error). Well-formed histories
    pass through unchanged."""
    if not messages:
        return messages

    uses = [_block_ids(m.get("content"), "tool_use", "id") for m in messages]
    results = [_block_ids(m.get("content"), "tool_result", "tool_use_id") for m in messages]

    out: list = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        prev_uses = uses[i - 1] if i > 0 else set()
        next_results = results[i + 1] if i + 1 < len(messages) else set()
        kept = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result" and b.get("tool_use_id") not in prev_uses:
                    continue  # tool_result with no matching tool_use in the prior message
                if b.get("type") == "tool_use" and b.get("id") not in next_results:
                    continue  # tool_use never answered by a tool_result in the next message
            kept.append(b)
        if kept == content:
            out.append(m)
        else:
            out.append({**m, "content": kept or [{"type": "text", "text": "(omitted)"}]})
    return out


async def compact_messages_with_claude(
    messages: list,
    model_id: str,
    session_id: str = "",
) -> list:
    """Intelligent context compaction with 3 strategies:

    1. Session memory shortcut — if a session memory file exists, use it as
       the summary instead of calling the LLM (zero API cost).
    2. LLM summarization — call Claude to produce a summary of old messages.
    3. Simple truncation — fallback if LLM call fails.
    """
    if len(messages) < 6:
        return messages

    # Pre-pass: smarter microcompact
    messages = microcompact_messages(messages)

    keep_recent = min(8, len(messages))
    # Split on a tool_use/tool_result-safe boundary so the recent window never
    # begins with an orphaned tool_result (which Bedrock rejects non-retryably,
    # defeating the very compaction meant to rescue the turn).
    split = _safe_recent_split(messages, keep_recent)
    if split == 0:
        # No boundary to summarize behind without orphaning a tool_result;
        # leave the history intact rather than build an invalid request.
        return messages
    old_messages = messages[:split]
    recent_messages = messages[split:]

    # Strategy 1: Session memory shortcut (zero API cost)
    if session_id:
        try:
            from server.memory.session_memory import load_session_memory

            session_mem = load_session_memory(session_id)
            if session_mem and len(session_mem) > 50:
                summary_msg = {
                    "role": "user",
                    "content": (
                        "[Context summary from session memory]\n"
                        f"{session_mem}\n\n"
                        "[End of session memory, recent messages follow]"
                    ),
                }
                log.info(
                    "Compacted %d old messages using session memory (%d chars)",
                    len(old_messages),
                    len(session_mem),
                )
                return [summary_msg] + recent_messages
        except Exception as e:
            log.warning("session memory compaction failed, falling back to LLM summary: %s", e)

    # Strategy 2: LLM summarization
    history_text = []
    for msg in old_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            history_text.append(f"{role.upper()}: {content[:1000]}")
        elif isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            history_text.append(f"{role.upper()}: {' '.join(parts)[:1000]}")

    if not history_text:
        return recent_messages

    summary_prompt = (
        "Summarize the following conversation history concisely. "
        "Preserve: (1) primary user intent, (2) key decisions made, "
        "(3) files touched or created, (4) errors encountered, "
        "(5) pending tasks. Keep it under 500 words. "
        "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen:\n\n"
        + "\n\n".join(history_text)
    )

    try:
        # Resolve at call time to dodge the __init__.py partial-init window
        # (this module is imported BEFORE `executor` is bound on the package).
        from server.chat import executor as _executor

        bedrock = _get_bedrock_client()
        loop = asyncio.get_event_loop()

        def _call():
            resp = bedrock.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 1024,
                        "system": (
                            "You produce concise conversation summaries. "
                            "Do not use em dashes or en dashes; prefer commas, "
                            "parentheses, a colon, or a short spaced hyphen."
                        ),
                        "messages": [{"role": "user", "content": summary_prompt}],
                    }
                ),
            )
            result = json.loads(resp["body"].read())
            return result.get("content", [{}])[0].get("text", "")

        summary = await loop.run_in_executor(_executor, _call)
        if summary:
            # Persist this summary as session memory for future compactions
            if session_id:
                try:
                    from server.memory.session_memory import get_session_memory_path

                    path = get_session_memory_path(session_id)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(summary)
                except Exception as e:
                    log.debug("could not persist compaction summary to session memory: %s", e)

            summary_msg = {
                "role": "user",
                "content": f"[Context summary of earlier conversation]\n{summary}",
            }
            log.info(
                "Compacted %d old messages into summary (%d chars)", len(old_messages), len(summary)
            )
            return [summary_msg] + recent_messages
    except Exception as e:
        log.warning("Claude compaction failed, falling back to truncation: %s", e)

    # Strategy 3: Simple truncation fallback
    return _compact_messages_simple(messages, model_id)


def _compact_messages_simple(messages: list, model_id: str) -> list:
    """Simple truncation-based compaction (fallback)."""
    if len(messages) < 4:
        return messages
    keep_recent = min(8, len(messages))
    trimmed = []
    for i, msg in enumerate(messages):
        is_recent = i >= len(messages) - keep_recent
        content = msg.get("content", "")
        if is_recent:
            trimmed.append(msg)
            continue
        if isinstance(content, str) and len(content) > 2000:
            trimmed.append({"role": msg["role"], "content": content[:2000] + "\n... (trimmed)"})
        elif isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        c = block.get("content", "")
                        if isinstance(c, str) and len(c) > 500:
                            new_blocks.append({**block, "content": c[:500] + "\n... (trimmed)"})
                        else:
                            new_blocks.append(block)
                    elif block.get("type") == "text" and len(block.get("text", "")) > 2000:
                        new_blocks.append(
                            {**block, "text": block["text"][:2000] + "\n... (trimmed)"}
                        )
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            trimmed.append({"role": msg["role"], "content": new_blocks})
        else:
            trimmed.append(msg)

    while len(trimmed) > 4 and estimate_message_size(trimmed) > MAX_CONTEXT_CHARS:
        trimmed = trimmed[2:]

    # Front-trimming can leave a dangling tool_result at the head; never start
    # a history on one.
    return ensure_valid_start(trimmed)
