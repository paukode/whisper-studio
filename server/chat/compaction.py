"""Fail-closed context compaction with a pure pruning pre-pass.

A summary uses exactly one selected model. Failed, incomplete, oversized or
non-shrinking summaries never replace the caller's history.
"""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy

log = logging.getLogger("whisper-studio")


class CompactionError(RuntimeError):
    """Compaction could not safely replace history; callers must stop or retry."""

    def __init__(self):
        super().__init__(
            "Context compaction failed; conversation history is unchanged. "
            "Check the compaction model and its availability, then retry, "
            "or start a new conversation with less context."
        )


def thresholds_for(model_key: str, meta: dict | None = None) -> tuple[int, int]:
    """Return the estimated trigger and hard ceiling in chars for a model. A
    model with no resolvable window raises (server/chat/engine/windows.py)."""
    from server.chat.engine.windows import compact_thresholds_chars

    return compact_thresholds_chars(model_key, meta)


def _content_size(content) -> int:
    """Count text, nested results, tool arguments and attachment payloads."""
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
            if "input" in block:
                total += len(json.dumps(block["input"], ensure_ascii=False, sort_keys=True))
            total += _content_size(block.get("content", ""))
    return total


def estimate_message_size(messages: list) -> int:
    return sum(_content_size(msg.get("content", "")) + 20 for msg in messages)


def microcompact_messages(messages: list, keep_recent: int = 6) -> list:
    """Pure age-tiered pruning of old string tool results, never a failure fallback."""
    messages = deepcopy(messages)
    total = len(messages)
    if total <= keep_recent:
        return messages
    old_count = total - keep_recent
    tier1_end = old_count // 3
    tier2_end = (old_count * 2) // 3
    for i, message in enumerate(messages[:old_count]):
        content = message.get("content", "")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = block.get("content", "")
            if not isinstance(text, str):
                continue
            if i < tier1_end:
                candidate = f"[{text[:80].replace(chr(10), ' ')}... (removed for compaction)]"
            elif i < tier2_end:
                candidate = text[:500] + "\n...[truncated]" if len(text) > 500 else text
            else:
                candidate = text[:2000] + "\n...[truncated]" if len(text) > 2000 else text
            if len(candidate) < len(text):
                block["content"] = candidate
    return messages


def _is_clean_user_start(msg: dict) -> bool:
    """Whether a recent suffix begins on a user turn without orphaned results."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return True


def _safe_recent_split(messages: list, keep_recent: int) -> int:
    """Grow the recent suffix backwards to a clean user turn, or return zero."""
    split = max(0, len(messages) - keep_recent)
    while split > 0 and not _is_clean_user_start(messages[split]):
        split -= 1
    return split


def ensure_valid_start(messages: list) -> list:
    """Drop leading messages until the list starts on a clean user turn."""
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
    """Drop orphaned tool blocks, using original immediate neighbours.

    This send-time repair helper is not part of compaction. Empty messages
    retain their role with a placeholder; well-formed histories are unchanged.
    """
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
                    continue
                if b.get("type") == "tool_use" and b.get("id") not in next_results:
                    continue
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
    model_key: str = "",
    trigger: str = "pressure",
) -> list:
    """Return a safe replacement or raise CompactionError, never mutate history.

    The historical name/signature remains for callers. model_id is not a
    fallback route. Cancellation propagates after best-effort error telemetry.
    Thresholds are estimates of history size, not a claim about the full wire
    request (system prompt, tools and provider tokenization are caller-owned).
    """
    from server.chat.compaction_log import record_end, record_start

    before_chars = 0

    def record(callback, **kwargs):
        try:
            callback(session_id, **kwargs)
        except Exception:  # noqa: BLE001 - diagnostics cannot change the outcome
            log.debug("Compaction diagnostics unavailable")

    outcome = "error"
    after_chars = before_chars
    try:
        before_chars = estimate_message_size(messages)
        after_chars = before_chars
        record(record_start, trigger=trigger, before_chars=before_chars)
        result, outcome = await _compact_strategies(
            messages, model_id, session_id, model_key, trigger
        )
        after_chars = estimate_message_size(result)
        return result
    except asyncio.CancelledError:
        outcome = "error"
        raise
    except CompactionError:
        outcome = "error"
        raise
    except Exception:  # noqa: BLE001 - no provider response or prompt in public errors
        outcome = "error"
        raise CompactionError() from None
    finally:
        record(record_end, outcome=outcome, after_chars=after_chars)


# The newest messages every compaction keeps exactly as they are.
KEEP_RECENT = 8


def _last_turn_start(messages: list) -> int | None:
    """The index of the last clean user turn (the prompt the current turn
    answers), or None."""
    return next(
        (i for i in range(len(messages) - 1, -1, -1) if _is_clean_user_start(messages[i])), None
    )


def _summary_cut(messages: list, keep_recent: int) -> tuple[int, int | None]:
    """Where a summary may cut, and the turn's prompt index when the cut is
    inside the last turn (else None).

    A clean cut starts the kept part on a user turn. One long turn (a single
    prompt and many tool rounds) has no such cut near the end, so the cut may
    also fall inside the last turn: on an assistant message whose previous
    message is a user message, so no tool pair is split. The cut closest to
    the end that keeps ``keep_recent`` messages wins."""
    clean = _safe_recent_split(messages, keep_recent)
    turn = _last_turn_start(messages)
    if turn is None:
        return clean, None
    limit = len(messages) - keep_recent
    in_turn = next(
        (
            c
            for c in range(limit, turn + 1, -1)
            if messages[c].get("role") == "assistant" and messages[c - 1].get("role") == "user"
        ),
        0,
    )
    if in_turn > clean:
        return in_turn, turn
    return clean, None


def _prompt_with_summary(prompt: dict, summary: str) -> dict:
    """The turn's prompt kept verbatim, with the summary of the earlier
    conversation and of the turn's earlier work after it."""
    content = prompt.get("content")
    if isinstance(content, str):
        return {**prompt, "content": f"{content}\n\n{summary}"}
    return {**prompt, "content": [*deepcopy(content), {"type": "text", "text": summary}]}


async def _compact_strategies(
    messages: list,
    model_id: str,
    session_id: str,
    model_key: str,
    trigger: str = "pressure",
) -> tuple[list, str]:
    """Try pure pruning, then one summary, with no fallback.

    Pruning shortens old tool results everywhere but the newest
    ``KEEP_RECENT`` messages, inside the current turn too. A summary cuts at
    a clean user turn, or inside one long turn (see _summary_cut), and then
    keeps that turn's prompt verbatim before the summary."""
    messages = deepcopy(messages)
    before_chars = estimate_message_size(messages)
    trigger_chars, hard_ceiling = thresholds_for(model_key)
    keep = min(KEEP_RECENT, len(messages))
    pruned = microcompact_messages(messages, keep_recent=keep)
    pruned_chars = estimate_message_size(pruned)
    if (
        trigger != "context-overflow"
        and pruned_chars <= min(trigger_chars, hard_ceiling)
        and pruned_chars < before_chars
    ):
        return pruned, "pruned-only"
    split, turn = _summary_cut(messages, keep)
    if split == 0:
        if trigger == "context-overflow":
            raise CompactionError()
        return messages, "unchanged"

    # Pruning was insufficient. The summary sees the original text and tool
    # arguments/results when they fit the summary model, else the pruned old
    # part (one long turn can be larger than any model's window).
    old_messages, recent_messages = messages[:split], messages[split:]
    if not _summary_input_fits(old_messages, model_key):
        old_messages = pruned[:split]
    # The kept prompt is not repeated in the summary's verbatim user excerpts.
    extras_from = None if turn is None else [m for i, m in enumerate(old_messages) if i != turn]
    summary = await summarize_messages(old_messages, model_key, session_id, extras_from=extras_from)
    if turn is None:
        result = [{"role": "user", "content": summary}, *recent_messages]
    else:
        result = [_prompt_with_summary(messages[turn], summary), *recent_messages]
    after_chars = estimate_message_size(result)
    if after_chars >= before_chars or after_chars > hard_ceiling:
        raise CompactionError()
    return result, "summary"


SUMMARY_HEADER = "[Context summary of earlier conversation]"


_SUMMARY_INSTRUCTIONS = (
    "Summarize the following conversation history concisely. Preserve user intent, "
    "constraints, decisions, files, tool calls and results, errors, and pending tasks. "
    "Keep it under 500 words. Attachment omission markers are references, not image "
    "or document contents; do not invent their contents.\n\n"
)
_SUMMARY_SYSTEM = (
    "You produce concise conversation summaries. Do not use em dashes or en dashes; "
    "prefer commas, parentheses, a colon, or a short spaced hyphen."
)


def _summary_input_fits(messages: list, model_key: str) -> bool:
    """Whether a summary of ``messages`` (with no note) fits the compaction
    model's window, measured the way summarize_messages measures it."""
    size = len(_SUMMARY_SYSTEM) + len(_SUMMARY_INSTRUCTIONS) + 40
    size += len(_serialize_history(messages))
    return size <= thresholds_for(_summary_model_key(model_key))[1]


async def summarize_messages(
    messages: list,
    model_key: str,
    session_id: str = "",
    note: str = "",
    extras_from: list | None = None,
) -> str:
    """One summary of ``messages`` by the compaction model, as the user message
    that stands in for them (header, summary, deterministic extras). ``note``
    is what the user asked the summary to keep. ``extras_from`` is the part
    the extras are taken from, when not all of ``messages``. Raises
    CompactionError."""
    summary_key = _summary_model_key(model_key)
    extras = _summary_extras(messages if extras_from is None else extras_from, session_id)
    keep = (
        f"The user asked the summary to keep this in particular: {note.strip()}\n\n"
        if note.strip()
        else ""
    )
    summary_prompt = _SUMMARY_INSTRUCTIONS + keep + _serialize_history(messages)
    summary_system = _SUMMARY_SYSTEM
    if len(summary_system) + len(summary_prompt) + 40 > thresholds_for(summary_key)[1]:
        raise CompactionError()

    from server.infrastructure.oneshot import one_shot
    from server.infrastructure.own_thread import run_on_own_thread
    from server.local.runtime import is_local_model

    local = is_local_model(summary_key)

    def call():
        return one_shot(
            summary_system,
            summary_prompt,
            max_tokens=1024,
            engine=summary_key if local else "cloud",
            cloud_model_key=summary_key,
            feature=_SUMMARY_FEATURE,
            source="compaction",
            session_id=session_id or "",
        )

    summary = await run_on_own_thread(call, name="compaction-summary")
    if not isinstance(summary, str) or not summary.strip():
        raise CompactionError()
    return f"{SUMMARY_HEADER}\n{summary.strip()}\n\n{extras}".rstrip()


async def compact_on_request(
    messages: list, model_key: str, session_id: str, note: str = ""
) -> tuple[str, int, int]:
    """The /compact command: always one summary of the whole prompt history.
    Returns ``(summary, before_chars, after_chars)``; a summary that does not
    shrink the history raises CompactionError, so nothing is replaced."""
    from server.chat.compaction_log import record_end, record_start

    before = estimate_message_size(messages)
    outcome, after = "error", before
    try:
        record_start(session_id, trigger="manual", before_chars=before)
    except Exception:  # noqa: BLE001 - diagnostics cannot change the outcome
        log.debug("Compaction diagnostics unavailable")
    try:
        if not messages:
            raise CompactionError()
        summary = await summarize_messages(messages, model_key, session_id, note)
        after = estimate_message_size([{"role": "user", "content": summary}])
        if after >= before:
            raise CompactionError()
        outcome = "summary"
        return summary, before, after
    except (CompactionError, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - no provider response or prompt in public errors
        raise CompactionError() from None
    finally:
        try:
            record_end(session_id, outcome=outcome, after_chars=after)
        except Exception:  # noqa: BLE001
            log.debug("Compaction diagnostics unavailable")


def _serialize_history(messages: list) -> str:
    """Deterministic lossless JSON for text/tool blocks; binary gets a reference.

    No arbitrary values are stringified, and nothing is length-clipped. An
    inline binary payload is identified by its original position and digest.
    External attachment references and other metadata remain verbatim.
    """

    def visit(value, path, binary_source=False):
        if isinstance(value, list):
            return [visit(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, dict):
            binary_source = binary_source and value.get("type") == "base64"
            attachment = value.get("type") in ("image", "document", "audio", "video")
            out = {}
            for key, item in value.items():
                item_path = f"{path}.{key}"
                if binary_source and key == "data":
                    if not isinstance(item, str):
                        raise CompactionError()
                    digest = hashlib.sha256(item.encode("utf-8")).hexdigest()
                    out[key] = (
                        f"[binary attachment omitted; reference={item_path}; sha256={digest}]"
                    )
                elif key == "input":
                    # Tool arguments are opaque JSON, never attachment blocks.
                    out[key] = item
                else:
                    out[key] = visit(item, item_path, attachment and key == "source")
            return out
        return value

    return json.dumps(
        visit(messages, "history"), ensure_ascii=False, sort_keys=True, allow_nan=False
    )


def _summary_extras(old_messages: list, session_id: str) -> str:
    """Keep verbatim user excerpts, anchors and recovery pointer; errors propagate."""
    from server.chat.compaction_anchors import render_summary_extras
    from server.infrastructure.feature_flags import is_enabled

    pointer_sid = session_id if is_enabled("session_search") else ""
    extras = render_summary_extras(old_messages, pointer_sid)
    return f"{extras}\n\n" if extras else ""


_SUMMARY_FEATURE = "Context compaction"


def _summary_model_key(model_key: str) -> str:
    """Resolve compaction's exact key; only missing settings default to main."""
    try:
        from server.infrastructure.config import load_config
        from server.local.runtime import is_local_model

        config = load_config()
        auxiliary = config.get("auxiliary_models", {})
        if not isinstance(auxiliary, dict):
            raise CompactionError()
        key = auxiliary.get("compaction", "main")
        if not isinstance(key, str) or not key.strip():
            raise CompactionError()
        key = model_key if key == "main" else key
        if not isinstance(key, str) or not key.strip():
            raise CompactionError()
        models = config.get("chat_models", {})
        if not isinstance(models, dict) or (key not in models and not is_local_model(key)):
            raise CompactionError()
        return key
    except CompactionError:
        raise
    except Exception:  # noqa: BLE001 - config read failures must not choose another model
        raise CompactionError() from None
