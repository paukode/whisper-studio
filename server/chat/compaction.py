"""Context compaction: clear old tool output first, then one structured summary.

A summary uses exactly one selected model. A failed, empty, oversized or
non-shrinking summary never replaces the caller's history. When the window
still has room (every trigger but a real overflow), a failed summary does not
end the turn: the turn goes on with the cleared history.
"""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy

log = logging.getLogger("whisper-studio")


class CompactionError(RuntimeError):
    """Compaction could not safely replace history; callers must stop or retry."""

    def __init__(self, reason: str = ""):
        # ``reason`` is for the log and the compaction record, not for the user.
        self.reason = reason
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


def _content_size(content, openai: bool = False) -> int:
    """Count text, nested results, tool arguments and attachment payloads. A
    picture counts at what the model bills for its pixels (image_size.py),
    not at the length of its data."""
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
            if block.get("type") == "thinking":
                total += len(block.get("thinking") or "")
            source = block.get("source")
            if isinstance(source, dict):
                data = source.get("data", "")
                if block.get("type") == "image":
                    from server.chat.image_size import image_chars

                    total += image_chars(data, openai)
                else:
                    total += len(data)
            if "input" in block:
                total += len(json.dumps(block["input"], ensure_ascii=False, sort_keys=True))
            total += _content_size(block.get("content", ""), openai)
    return total


def estimate_message_size(messages: list, model_key: str = "") -> int:
    """The history's size in chars at 4 chars a token. ``model_key`` picks the
    picture rule (GPT patches for an OpenAI model, Claude patches otherwise)."""
    openai = False
    if model_key:
        from server.openai_bedrock.runtime import is_openai_model

        openai = is_openai_model(model_key)
    return sum(_content_size(msg.get("content", ""), openai) + 20 for msg in messages)


PICTURE_CLEARED = "[picture removed for compaction]"


# A request may not carry pictures without end: the provider limits their
# number (100 in one request for a 200k-token model) and the size of the
# request, and a picture is cheap in tokens, so the size estimate does not
# stop them. Over MAX_TOOL_PICTURES pictures from tools, the oldest are
# cleared down to KEEP_TOOL_PICTURES. The step is large on purpose: the
# history changes once in many rounds, not in each round (prompt cache).
MAX_TOOL_PICTURES = 60
KEEP_TOOL_PICTURES = 30


def _tool_picture_slots(messages: list) -> list[tuple[list, int]]:
    """Each picture a tool returned, oldest first, as (its part list, index)."""
    slots = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or message.get("role") != "user":
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            parts = block.get("content")
            if isinstance(parts, list):
                slots += [
                    (parts, i)
                    for i, part in enumerate(parts)
                    if isinstance(part, dict) and part.get("type") == "image"
                ]
    return slots


def limit_tool_pictures(messages: list) -> list:
    """The history with at most MAX_TOOL_PICTURES pictures from tools. Under
    the limit the same list comes back; over it, a copy whose oldest tool
    pictures are markers. Pictures the user attached stay."""
    if len(_tool_picture_slots(messages)) <= MAX_TOOL_PICTURES:
        return messages
    messages = deepcopy(messages)
    slots = _tool_picture_slots(messages)
    for parts, i in slots[: len(slots) - KEEP_TOOL_PICTURES]:
        parts[i] = {"type": "text", "text": PICTURE_CLEARED}
    return messages


def _clip_result_text(text: str, tier: int) -> str:
    """An old tool result's text at its age tier (0 oldest), never longer."""
    if tier == 0:
        candidate = f"[{text[:80].replace(chr(10), ' ')}... (removed for compaction)]"
    elif tier == 1:
        candidate = text[:500] + "\n...[truncated]" if len(text) > 500 else text
    else:
        candidate = text[:2000] + "\n...[truncated]" if len(text) > 2000 else text
    return candidate if len(candidate) < len(text) else text


def microcompact_messages(messages: list, keep_recent: int = 6) -> list:
    """Pure age-tiered clearing of old tool results. Text is shortened by
    age. A picture a tool returned (a screenshot) is replaced by a marker at
    every age: it showed a state that later rounds replaced. Pictures the
    user attached are not tool results and stay."""
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
        tier = 0 if i < tier1_end else 1 if i < tier2_end else 2
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result = block.get("content", "")
            if isinstance(result, str):
                block["content"] = _clip_result_text(result, tier)
            elif isinstance(result, list):
                block["content"] = [_clear_result_part(part, tier) for part in result]
    return messages


def _clear_result_part(part, tier: int):
    if not isinstance(part, dict):
        return part
    if part.get("type") == "image":
        return {"type": "text", "text": PICTURE_CLEARED}
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        return {**part, "text": _clip_result_text(part["text"], tier)}
    return part


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
    """Return a safe replacement, never mutate history.

    Raises CompactionError only when the request really overflowed
    (``trigger="context-overflow"``) and no summary could shrink it. On every
    other trigger a failed summary returns the cleared history (or the
    history as it was) so the turn goes on.

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

    outcome, detail = "error", ""
    after_chars = before_chars
    # A summary of this history failed a moment ago. Clearing it again each
    # round would change old rows each round for nothing (prompt cache).
    if summary_held(session_id, messages, model_key, trigger):
        return messages
    try:
        before_chars = estimate_message_size(messages, model_key)
        after_chars = before_chars
        record(record_start, trigger=trigger, before_chars=before_chars)
        result, outcome, detail = await _compact_strategies(
            messages, model_id, session_id, model_key, trigger
        )
        after_chars = estimate_message_size(result, model_key)
        return result
    except asyncio.CancelledError:
        outcome, detail = "error", "cancelled"
        raise
    except CompactionError as exc:
        outcome, detail = "error", exc.reason
        log.warning("Compaction failed (%s): %s", trigger, exc.reason or "no reason given")
        raise
    except Exception as exc:  # noqa: BLE001 - no provider response or prompt in public errors
        outcome, detail = "error", _failure_reason(exc)
        log.warning("Compaction failed (%s): %s", trigger, detail)
        raise CompactionError(detail) from None
    finally:
        record(record_end, outcome=outcome, after_chars=after_chars, detail=detail)


def _failure_reason(exc: BaseException) -> str:
    """Why a compaction step failed, for the log and the compaction record."""
    if isinstance(exc, CompactionError):
        return exc.reason or "compaction refused the result"
    text = " ".join(str(exc).split())[:200]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# After a failed summary the same history is not summarized again each round
# (one call reads the whole history): the next attempt waits until the
# history grew by this factor.
SUMMARY_RETRY_GROWTH = 1.1
_MAX_HELD = 256
_summary_hold: dict[str, int] = {}


def _hold_summary(session_id: str, size: int) -> None:
    if not session_id:
        return
    _summary_hold.pop(session_id, None)
    _summary_hold[session_id] = size
    if len(_summary_hold) > _MAX_HELD:
        _summary_hold.pop(next(iter(_summary_hold)), None)


def _summary_on_hold(session_id: str, size: int) -> bool:
    held = _summary_hold.get(session_id)
    return held is not None and size < held * SUMMARY_RETRY_GROWTH


def summary_held(session_id: str, messages: list, model_key: str, trigger: str) -> bool:
    """True when compaction has nothing to do now: the last summary of this
    session failed and the history did not grow enough since. A real
    overflow is never held."""
    if trigger == "context-overflow" or session_id not in _summary_hold:
        return False
    return _summary_on_hold(session_id, estimate_message_size(messages, model_key))


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
) -> tuple[list, str, str]:
    """Clear old tool output, then one summary. Returns the new history, the
    outcome and a detail (the reason, when a summary failed).

    Clearing shortens old tool results and removes old tool pictures
    everywhere but the newest ``KEEP_RECENT`` messages, inside the current
    turn too. A summary cuts at a clean user turn, or inside one long turn
    (see _summary_cut), and then keeps that turn's prompt verbatim before the
    summary. A summary that fails while the window still has room leaves the
    cleared history; only a real overflow raises."""
    messages = deepcopy(messages)

    def size(history: list) -> int:
        return estimate_message_size(history, model_key)

    before_chars = size(messages)
    trigger_chars, hard_ceiling = thresholds_for(model_key)
    keep = min(KEEP_RECENT, len(messages))
    pruned = microcompact_messages(messages, keep_recent=keep)
    pruned_chars = size(pruned)
    overflow = trigger == "context-overflow"
    cleared = pruned if pruned_chars < before_chars else messages
    cleared_outcome = "pruned-only" if cleared is pruned else "unchanged"
    # "token-pressure": the real prompt size asked for this while the estimate
    # is under its trigger, so the estimate cannot say clearing was enough.
    by_estimate = trigger != "token-pressure"
    if (
        not overflow
        and by_estimate
        and cleared is pruned
        and pruned_chars <= min(trigger_chars, hard_ceiling)
    ):
        return pruned, "pruned-only", ""
    split, turn = _summary_cut(messages, keep)
    if split == 0:
        if overflow:
            raise CompactionError("no place to cut the history for a summary")
        return cleared, cleared_outcome, ""
    if not overflow and _summary_on_hold(session_id, before_chars):
        return cleared, cleared_outcome, "summary on hold after a failed summary"

    # Clearing was not enough. The summary sees the original text and tool
    # arguments/results when they fit the summary model, else the cleared old
    # part (one long turn can be larger than any model's window).
    try:
        old_messages, recent_messages = messages[:split], messages[split:]
        if not _summary_input_fits(old_messages, model_key):
            old_messages = pruned[:split]
        # The kept prompt is not repeated in the summary's verbatim user excerpts.
        extras_from = None if turn is None else [m for i, m in enumerate(old_messages) if i != turn]
        summary = await summarize_messages(
            old_messages, model_key, session_id, extras_from=extras_from
        )
        if turn is None:
            result = [{"role": "user", "content": summary}, *recent_messages]
        else:
            result = [_prompt_with_summary(messages[turn], summary), *recent_messages]
        after_chars = size(result)
        if after_chars >= before_chars:
            raise CompactionError("the summary did not make the history smaller")
        if after_chars > hard_ceiling:
            raise CompactionError("the history is over the window after the summary")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a summary failure must not end a turn with room
        reason = _failure_reason(exc)
        if overflow:
            if isinstance(exc, CompactionError):
                raise
            raise CompactionError(reason) from None
        _hold_summary(session_id, before_chars)
        log.warning(
            "Compaction summary failed (%s); the turn continues on the %s history",
            reason,
            "cleared" if cleared is pruned else "unchanged",
        )
        return cleared, "summary-failed", reason
    _summary_hold.pop(session_id, None)
    return result, "summary", ""


SUMMARY_HEADER = "[Context summary of earlier conversation]"


# The note is a handover: the assistant that continues reads only the note.
_SUMMARY_INSTRUCTIONS = (
    "Write the handover note for the conversation history below. The note replaces "
    "that history: the assistant that continues the work reads only the note. Use "
    "these sections, in this order, and write None for a section with nothing in it.\n"
    "1. Context: what the user wants and why, the constraints and preferences they "
    "gave, and the files, folders, tools and names in play.\n"
    "2. Decisions: each decision, who made it (the user or the assistant), and the "
    "reason.\n"
    "3. Outcomes: what was done, with the file paths and exact values later work "
    "needs (sizes, names, commands, versions). For each outcome say how it was "
    "checked, or that it was not checked. Include each error and how it was fixed.\n"
    "4. Assumptions: what was taken as true without proof, and open questions.\n"
    "5. Pending: work that was asked for and is not done.\n"
    "6. Current work: what was in progress at the end of this history, and the next "
    "step.\n"
    "Be exact and complete before short, and aim for under 1,500 words. The note must "
    "be shorter than the history it replaces: a short history gets a short note. Do not call "
    "tools. Attachment omission markers are references, not image or document "
    "contents; do not invent their contents.\n\n"
)
_SUMMARY_SYSTEM = (
    "You write handover notes of conversations. Do not use em dashes or en dashes; "
    "prefer commas, parentheses, a colon, or a short spaced hyphen."
)
# Room for the note. A note that still hits this cap is used as far as it got.
SUMMARY_MAX_TOKENS = 8192
SUMMARY_CUT_OFF = "[The summary was cut off here.]"


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
        raise CompactionError("the history to summarize is larger than the summary model's window")

    from server.infrastructure.oneshot import OneShotIncomplete, one_shot
    from server.infrastructure.own_thread import run_on_own_thread
    from server.local.runtime import is_local_model

    local = is_local_model(summary_key)

    def call():
        return one_shot(
            summary_system,
            summary_prompt,
            max_tokens=SUMMARY_MAX_TOKENS,
            engine=summary_key if local else "cloud",
            cloud_model_key=summary_key,
            feature=_SUMMARY_FEATURE,
            source="compaction",
            session_id=session_id or "",
        )

    try:
        summary = await run_on_own_thread(call, name="compaction-summary")
    except OneShotIncomplete as cut:
        # The note ran out of room. What it wrote is still a true note.
        if not cut.text.strip():
            raise CompactionError("the summary was cut off before any text") from None
        summary = f"{cut.text.strip()}\n{SUMMARY_CUT_OFF}"
    except CompactionError:
        raise
    except Exception as exc:  # noqa: BLE001 - the reason goes to the log, not to the user
        raise CompactionError(_failure_reason(exc)) from None
    if not isinstance(summary, str) or not summary.strip():
        raise CompactionError("the summary model returned no text")
    return f"{SUMMARY_HEADER}\n{summary.strip()}\n\n{extras}".rstrip()


# Why /compact refuses a short conversation (compact_route.py says it plainly).
NOT_SMALLER = "the summary was not smaller than the history"


async def compact_on_request(
    messages: list, model_key: str, session_id: str, note: str = ""
) -> tuple[str, int, int]:
    """The /compact command: always one summary of the whole prompt history.
    Returns ``(summary, before_chars, after_chars)``; a summary that does not
    shrink the history raises CompactionError, so nothing is replaced."""
    from server.chat.compaction_log import record_end, record_start

    before = estimate_message_size(messages)
    outcome, after, detail = "error", before, ""
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
            raise CompactionError(NOT_SMALLER)
        outcome = "summary"
        return summary, before, after
    except CompactionError as exc:
        detail = exc.reason
        log.warning("Compaction on request failed: %s", detail or "no reason given")
        raise
    except asyncio.CancelledError:
        detail = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - no provider response or prompt in public errors
        detail = _failure_reason(exc)
        log.warning("Compaction on request failed: %s", detail)
        raise CompactionError(detail) from None
    finally:
        try:
            record_end(session_id, outcome=outcome, after_chars=after, detail=detail)
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
            # A thinking block's signature (and redacted thinking) is opaque
            # data for the provider: it says nothing a summary can use.
            opaque = {"thinking": "signature", "redacted_thinking": "data"}.get(value.get("type"))
            out = {}
            for key, item in value.items():
                if key == opaque:
                    continue
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
