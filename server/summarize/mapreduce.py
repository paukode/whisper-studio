"""Map-reduce condensation for oversized transcripts.

Transcript-driven tools and skills (the summarize_transcript built-in, plus
transcript-taking user skills) inject the whole transcript into the model's
context so it can summarise it in one pass. That is the best quality while the transcript fits the context window.
When it does not fit, the alternatives are to truncate it silently (a wrong
answer) or to overflow the request (a hard failure). Instead this module does
the "map" half of map-reduce: split the transcript into overlapping chunks,
extract the salient raw material from each with a fast one-shot model, and
concatenate the extracts. That condensed text is substituted in place of the
raw transcript, and the main chat model composes the requested summary from it
in its normal turn (the "reduce" half).

Only fires when the transcript is larger than the reading chat model can take:
what its input budget leaves beside the rest of the turn when the caller
measured that turn (``turn_tokens``), else a share of the budget after a fixed
prompt overhead, capped by a configurable threshold either way. Below that the
transcript passes through unchanged. The map step runs on the engine
the model mode picks (never a cloud engine in Local mode, never any model when
the hybrid one-shot writer is Off) and is sized for that engine; the
condensed output is sized for the reader. It never returns an empty string: on
failure it falls back to a transcript truncated to what the reader can take,
with a marker that says why, and that fallback is never cached.
"""

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable

from server.index.chunker import chunk_text
from server.infrastructure.config import load_config
from server.infrastructure.oneshot import OFF, one_shot, resolve_local_key, resolve_map_engine

# The frontend re-sends the whole transcript with every chat turn, and the raw
# transcript is injected into the prompt on every turn (not just summary turns),
# so an oversized transcript would otherwise be re-condensed (N map calls) on
# each message. Cache the condensed result by transcript hash + config signature
# so identical input is condensed once. Bounded and in-memory; a growing live
# recording changes the hash and re-condenses occasionally, which is intended.
_CACHE_MAX = 8
_cache: "OrderedDict[str, str]" = OrderedDict()
_lock = threading.Lock()

log = logging.getLogger("whisper-studio")

# Config lives under the top-level "map_reduce_summary" key; these are the
# defaults merged under any user overrides. Sizes are in characters; the chunker
# estimates ~4 chars/token.
_DEFAULTS = {
    "enabled": True,
    # Upper bound on what the reader gets raw (~150k tokens, a cost cap for 1M
    # models); a smaller reader condenses sooner (see _reader_limit).
    "threshold_chars": 600_000,
    "chunk_chars": 150_000,  # ~37k tokens per chunk (cloud map engine)
    "local_chunk_chars": 40_000,  # ~10k tokens per chunk (fits a 16k or 32k local window)
    "overlap_chars": 800,
    "map_max_tokens": 1200,
    "max_chunks": 40,
    # Caps on the condensed result when the reader is unknown (a skill argument):
    # by map engine. A known reader is capped by its own limit instead.
    "max_output_chars": 480_000,
    "local_max_output_chars": 24_000,
    # "auto" -> follow model mode; or force "haiku" / "local" (Local mode never
    # honours a forced "haiku": the map stays on-device there).
    "engine": "auto",
}

# When the turn is not measured: the prompt around a transcript is taken to be
# the system prompt plus a full tool pool (about 17K tokens on a tool-capable
# local model, see server/local/registry.py), and the transcript may take this
# share of what is left (the rest is history, the question and per-turn
# context). The same size bounds the condensed output and the truncation
# fallback, measured turn or not, so a cached condensation stays valid while
# the history grows.
_PROMPT_OVERHEAD_TOKENS = 20_000
_TRANSCRIPT_SHARE = 0.6
# When the turn is measured: held back from the budget for what the
# measurement cannot see yet (grounding passages, injected task completions)
# and for the tool rounds the turn may still run.
_TURN_RESERVE_FRACTION = 0.125
_TURN_RESERVE_MIN_TOKENS = 2_048
_CHARS_PER_TOKEN = 4

# Numeric config fields, coerced to int in _cfg so a malformed value (e.g. the
# string "600k") falls back to its default instead of raising in the hot path.
_INT_FIELDS = (
    "threshold_chars",
    "chunk_chars",
    "local_chunk_chars",
    "overlap_chars",
    "map_max_tokens",
    "max_chunks",
    "max_output_chars",
    "local_max_output_chars",
)

MAP_SYSTEM = (
    "You are extracting raw material from ONE segment of a longer transcript. "
    "Other segments are processed separately, so capture everything "
    "self-contained and do not rely on context from outside this segment.\n\n"
    "Extract, do not summarise. Keep the actual wording. Do not paraphrase into "
    "prose, do not compress meaning, and do not invent, infer, or add anything "
    "that is not present in this segment. If something is ambiguous, keep it and "
    "flag it.\n\n"
    "Output only the labelled sections below. Omit a section entirely if this "
    "segment has nothing for it. Do not pad.\n\n"
    "ATTENDEES/SPEAKERS: every distinct speaker name or label that appears.\n"
    "DECISIONS: each decision stated, quoted or close to verbatim.\n"
    'ACTION ITEMS: one per line as owner | task | deadline. Write "unspecified" '
    "for any field that is missing. Do not guess an owner.\n"
    "DISCUSSION POINTS: the key topics and substantive statements as short, near "
    "verbatim bullets.\n"
    "BLOCKERS: anything described as blocked, at risk, or waiting on something.\n"
    "OPEN QUESTIONS: questions raised and left unanswered in this segment.\n"
    "UNCERTAIN: names, numbers, dates, or terms that were unclear, misspelled, "
    "or possibly mis-transcribed, so a reader can verify them.\n\n"
    "Do not use emojis. Do not use dashes to join clauses; use commas, periods, "
    "or parentheses."
)

MAP_USER = "SEGMENT {i} of {n}:\n\n{chunk}"

# Prepended to the concatenated extracts so the main model knows it is composing
# from per-segment extracts, not the raw transcript.
NOTE_PREFIX = (
    "[NOTE: The transcript was too long to include in full. What follows is not "
    "the raw transcript but a set of per-segment extracts produced automatically "
    "from consecutive chunks of it. Fidelity is reduced: wording is approximate, "
    "order across segments is preserved but fine detail within a segment may be "
    "lost, and material near segment boundaries may be repeated. Compose the "
    "requested summary from this material. Where an owner, deadline, name, or "
    "number is marked unspecified or uncertain, say so rather than inventing a "
    "value, and note any gaps at the end.]\n\n"
)


def _cfg(config: dict | None = None) -> dict:
    """Defaults merged under the user's ``map_reduce_summary`` config block.

    Numeric fields are coerced to int so a malformed value never raises later in
    the hot path (the size gate and thresholds must not crash a chat turn)."""
    cfg = dict(_DEFAULTS)
    try:
        user = (config or load_config()).get("map_reduce_summary") or {}
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in _DEFAULTS})
    except Exception as e:  # never let a config read break summarisation
        log.warning("map_reduce_summary config read failed (%s); using defaults", e)
    for k in _INT_FIELDS:
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            log.warning("map_reduce_summary.%s is not an int (%r); using default", k, cfg[k])
            cfg[k] = _DEFAULTS[k]
    return cfg


def threshold(config: dict | None = None) -> int:
    """The character length above which a transcript gets condensed when the
    reader is unknown; a known reader with a smaller budget condenses sooner."""
    return int(_cfg(config)["threshold_chars"])


def maybe_condense_transcript(
    text: str,
    *,
    config: dict | None = None,
    chat_model_key: str | None = None,
    reader_n_ctx: int | None = None,
    turn_tokens: int | None = None,
) -> str:
    """Return ``text`` condensed to per-chunk extracts if it is larger than the
    reading chat model can take, otherwise unchanged.

    ``chat_model_key`` names the chat model that will read the result, cloud or
    on-device. It sizes the gate and the condensed output to that model's real
    input budget (:func:`_reader_limit`), and a local map step follows it when
    it is on-device instead of evicting it (see
    :func:`server.infrastructure.oneshot.resolve_local_key`). Without it (a
    skill argument), ``threshold_chars`` and the map engine's output cap apply.
    ``reader_n_ctx`` is the context size an on-device reader's turn asks for
    (the composer's CTX chip). The local route starts the model at that size
    only after this runs, so it wins over the resident or remembered size, and
    a local map step starts the model at it too.
    ``turn_tokens`` is what the reader's turn carries besides the transcript
    (:func:`reader_turn_tokens`). With it, the transcript stays raw while it
    fits beside that turn (:func:`_raw_fit`) instead of being held to the
    fixed-overhead limit.

    Never returns ``""``: on failure it falls back to the transcript truncated
    to the reader's limit, marked with the reason. Only a complete condensation
    is cached; a fallback or a result with a failed chunk is recomputed next
    time, so a mode switch, a model install or a transient error never pins a
    degraded transcript.
    """
    if not text or not text.strip():
        return text
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return text
    limit = _reader_limit(chat_model_key, cfg, reader_n_ctx)
    if len(text) <= _raw_fit(chat_model_key, cfg, reader_n_ctx, turn_tokens, limit):
        return text
    engine = _engine(cfg)
    if engine == OFF:
        return _hard_truncate(
            text,
            limit,
            "the one-shot writer is Off in Settings > Model mode, so it was not condensed",
        )
    max_out = _output_cap(chat_model_key, engine, cfg, limit, reader_n_ctx)
    key = _cache_key(text, cfg, _map_model(engine, chat_model_key), limit, max_out)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
    if cached is not None:
        return cached
    _pin_reader_ctx(chat_model_key, reader_n_ctx)
    # Run the (slow, blocking) map calls OUTSIDE the lock so concurrent requests
    # do not serialise; a duplicate miss just recomputes the same result.
    try:
        result, complete = _condense(
            text, cfg, engine=engine, max_out=max_out, chat_model_key=chat_model_key
        )
    except Exception as e:
        log.warning("transcript condensation failed (%s); truncating instead", e)
        return _hard_truncate(text, limit, f"condensing it failed: {e}")
    if complete:
        with _lock:
            _cache[key] = result
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
    return result


def _input_budget(model_key: str | None, n_ctx: int | None = None) -> int | None:
    """The reader's input budget in tokens, or None when unknown."""
    if not model_key:
        return None
    try:
        from server.chat.engine.windows import input_budget

        return input_budget(model_key, _requested_window_meta(model_key, n_ctx))
    except Exception as e:  # noqa: BLE001 - sizing must never break a chat turn
        log.debug("input budget for %s unavailable: %s", model_key, e)
        return None


def _requested_window_meta(model_key: str, n_ctx: int | None) -> dict | None:
    """Model meta pinned to the context size the turn asks for, when an
    on-device reader names one; None (the live window) otherwise. Clamped the
    way server/local/route.py clamps it before starting the model at it."""
    size = _clamped_ctx(n_ctx)
    if not size:
        return None
    from server.local import runtime as local_rt

    if not local_rt.is_local_model(model_key):
        return None
    meta = dict((load_config().get("chat_model_meta") or {}).get(model_key) or {})
    meta.update(is_local=True, context_window=size)
    return meta


def _clamped_ctx(n_ctx: int | None) -> int | None:
    """``n_ctx`` clamped the way server/local/route.py clamps the CTX chip."""
    if not n_ctx:
        return None
    try:
        return max(2048, min(int(n_ctx), 262144))
    except (TypeError, ValueError):
        return None


def _pin_reader_ctx(chat_model_key: str | None, n_ctx: int | None) -> None:
    """Record an on-device reader's requested context before a local map step
    can start the model, so it starts once at the size the turn asks for
    instead of at the remembered one and then again at the chip's (the local
    route records the same value right after this)."""
    size = _clamped_ctx(n_ctx)
    if not size:
        return
    from server.local import runtime as local_rt

    if local_rt.is_local_model(chat_model_key):
        local_rt.set_requested_n_ctx(size)


def _reader_limit(chat_model_key: str | None, cfg: dict, n_ctx: int | None = None) -> int:
    """How many transcript characters the reading chat model takes in one turn
    with the prompt overhead assumed, not measured: a share of its input budget
    after that overhead (floored at a quarter of the budget), never above
    ``threshold_chars``. ``threshold_chars`` alone when the reader is unknown.
    It bounds the condensed output and the truncation fallback, and gates the
    raw transcript when the turn was not measured."""
    cap = int(cfg["threshold_chars"])
    budget = _input_budget(chat_model_key, n_ctx)
    if not budget:
        return cap
    usable = max(budget - _PROMPT_OVERHEAD_TOKENS, budget // 4)
    return min(cap, int(usable * _CHARS_PER_TOKEN * _TRANSCRIPT_SHARE))


def _raw_fit(
    chat_model_key: str | None,
    cfg: dict,
    n_ctx: int | None,
    turn_tokens: int | None,
    limit: int,
) -> int:
    """The longest transcript the reader's turn takes raw. With the turn
    measured, what the input budget leaves after it and a reserve (never above
    ``threshold_chars``); otherwise ``limit``, the fixed-overhead size."""
    if turn_tokens is None:
        return limit
    budget = _input_budget(chat_model_key, n_ctx)
    if not budget:
        return limit
    reserve = max(_TURN_RESERVE_MIN_TOKENS, int(budget * _TURN_RESERVE_FRACTION))
    room = budget - max(0, int(turn_tokens)) - reserve
    return max(0, min(int(cfg["threshold_chars"]), room * _CHARS_PER_TOKEN))


def reader_turn_tokens(
    chat_model_key: str | None,
    *,
    system_prompt: str,
    tools: list[dict],
    messages: list,
    texts: Iterable[str] = (),
    local_prompt_parts: tuple[str, str, str] = ("", "", ""),
    ws_path: str = "",
) -> int:
    """What the reader's turn carries besides the transcript, in tokens at 4
    chars each: its system prompt, the tool schemas it is sent, the history and
    this turn's other text (the question, attachments).

    An on-device reader is measured the way server/local/route.py builds its
    turn: the lean local system prompt (from ``local_prompt_parts``, the
    WHISPER.md, memory and session memory context) instead of
    ``system_prompt``, and tools only when the model supports them."""
    from server.chat.compaction import estimate_message_size
    from server.chat.tool_index import estimate_tool_tokens
    from server.local import runtime as local_rt

    if local_rt.is_local_model(chat_model_key):
        tools_on = local_rt.supports_tools(chat_model_key)
        system_prompt = local_rt.build_local_system_prompt(
            *local_prompt_parts, tools=tools_on, ws_path=ws_path
        )
        if not tools_on:
            tools = []
    chars = len(system_prompt or "") + estimate_message_size(messages or [])
    chars += sum(len(t or "") for t in texts)
    return chars // _CHARS_PER_TOKEN + estimate_tool_tokens(tools or [])


def _output_cap(
    chat_model_key: str | None, engine: str, cfg: dict, limit: int, n_ctx: int | None = None
) -> int:
    """How large the condensed extracts may be. They take the transcript's
    place, so a known reader gets its own limit (never above
    ``max_output_chars``); an unknown reader gets the map engine's configured
    cap."""
    if _input_budget(chat_model_key, n_ctx):
        return min(int(cfg["max_output_chars"]), limit)
    if _is_local_engine(engine):
        return int(cfg["local_max_output_chars"])
    return int(cfg["max_output_chars"])


def _is_local_engine(engine: str) -> bool:
    """True for the "local" alias and for any specific on-device model key."""
    if engine == "local":
        return True
    from server.local import runtime as local_rt

    return bool(local_rt.is_local_model(engine))


def _map_model(engine: str, chat_model_key: str | None) -> str:
    """What actually runs the map: the on-device key the "local" alias resolves
    to, else the engine itself. Part of the cache key, so a different map model
    never serves another one's result."""
    if engine == "local":
        return resolve_local_key(chat_model_key)
    return engine


def _cache_key(text: str, cfg: dict, map_model: str, limit: int, max_out: int) -> str:
    # Every input that changes the condensed output must be in the signature:
    # the config fields, the RESOLVED map model (never the raw "auto"), and the
    # reader-derived sizes, or a mode switch, a model install or a different
    # chat model would keep returning a stale result.
    sig = ":".join(str(cfg[k]) for k in _INT_FIELDS)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return f"{digest}|{sig}|{map_model}|{limit}|{max_out}"


def _condense(
    text: str, cfg: dict, *, engine: str, max_out: int, chat_model_key: str | None = None
) -> tuple[str, bool]:
    """Map every chunk and join the extracts. Returns ``(text, complete)``;
    ``complete`` is False when any chunk's map call failed, so the caller does
    not cache a result with a hole in it. Raises when nothing was extracted."""
    local = _is_local_engine(engine)
    chunk_chars = cfg["local_chunk_chars"] if local else cfg["chunk_chars"]
    chunks = _chunk(text, chunk_chars, cfg["overlap_chars"], cfg["max_chunks"])
    if not chunks:
        raise RuntimeError("the transcript produced no chunks")
    n = len(chunks)
    errors: list[str] = []
    results = _map_all(chunks, engine, cfg, chat_model_key=chat_model_key, errors=errors)
    extracts = [r.strip() for r in results if r.strip()]
    if not extracts:
        # Every map call failed or came back empty; the caller truncates the
        # raw transcript rather than hand the model nothing.
        raise RuntimeError(errors[0] if errors else "every map extraction came back empty")
    condensed = "\n\n".join(extracts)
    # Bound the condensed result to what the reader takes, so the "reduce" turn
    # does not overflow. The map input is already sized for the map engine.
    if len(condensed) > max_out:
        log.warning(
            "condensed extracts (%d chars) exceed the output cap %d (engine=%s); truncating",
            len(condensed),
            max_out,
            engine,
        )
        condensed = (
            condensed[:max_out]
            + "\n\n[Some later segments omitted: the condensed notes exceeded the target size.]"
        )
    log.info(
        "transcript condensed: %d chars -> %d chunks -> %d chars of extracts (engine=%s)",
        len(text),
        n,
        len(condensed),
        engine,
    )
    return NOTE_PREFIX + condensed, len(extracts) == n and not errors


def _map_all(
    chunks: list[str],
    engine: str,
    cfg: dict,
    *,
    chat_model_key: str | None = None,
    errors: list[str] | None = None,
) -> list[str]:
    """Run the per-chunk map calls, preserving order. The chunks are independent
    by construction (each extraction prompt is self-contained), so the cloud
    path fans them out with bounded concurrency to cut first-token latency. The
    local path (the alias or a specific on-device key) stays serial: a single
    llama.cpp instance cannot run concurrent completions. A failed chunk's
    reason is appended to ``errors``."""
    n = len(chunks)

    def run(i: int, c: str) -> str:
        return _map_chunk(c, i, n, engine, cfg, chat_model_key=chat_model_key, errors=errors)

    if n == 1 or _is_local_engine(engine):
        return [run(i, c) for i, c in enumerate(chunks, 1)]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, n)) as pool:
        # map() preserves input order, so the concatenation order stays stable.
        return list(pool.map(lambda it: run(*it), enumerate(chunks, 1)))


def _engine(cfg: dict) -> str:
    """The map engine: a forced ``haiku``/``local`` from config, else what the
    model mode resolves (``haiku``, ``local``, an on-device key, or ``none``
    for Off). Local mode never maps on a cloud engine, forced or not: the
    on-device path is used there."""
    choice = str(cfg.get("engine") or "auto").strip()
    if choice == "local":
        return choice
    if choice == "haiku":
        from server.infrastructure.cloud_guard import cloud_allowed

        if cloud_allowed():
            return choice
        log.info("map_reduce_summary.engine=haiku is a cloud engine; Local mode maps on-device")
    return resolve_map_engine()


def _chunk(text: str, chunk_chars: int, overlap_chars: int, max_chunks: int) -> list[str]:
    """Split ``text`` into overlapping chunks sized in characters. Reuses the
    index chunker (line-anchored, structure-aware). If the split exceeds
    ``max_chunks``, re-chunk coarsely so the whole transcript is still covered
    rather than dropping its tail; a hard cap is the last resort."""

    def split(cc: int) -> list[str]:
        max_tokens = max(1, cc // 4)
        overlap_tokens = min(max(0, overlap_chars // 4), max_tokens // 4)
        out: list[str] = []
        for c in chunk_text(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens):
            piece = c["text"]
            # chunk_text is line-anchored: a newline-sparse input (one giant
            # line) yields a single oversized chunk. Hard-split by characters so
            # no chunk overflows the map model window.
            if len(piece) > cc:
                out.extend(piece[j : j + cc] for j in range(0, len(piece), cc))
            else:
                out.append(piece)
        return out

    chunks = split(chunk_chars)
    if len(chunks) > max_chunks:
        coarse = -(-len(text) // max_chunks)  # ceil division
        log.warning(
            "transcript of %d chars produced %d chunks at %d-char chunks (max_chunks=%d); "
            "re-chunking at ~%d chars each",
            len(text),
            len(chunks),
            chunk_chars,
            max_chunks,
            coarse,
        )
        chunks = split(coarse)
        if len(chunks) > max_chunks:
            log.warning(
                "still %d chunks after re-chunk; hard-capping to %d (tail not condensed)",
                len(chunks),
                max_chunks,
            )
            chunks = chunks[:max_chunks]
    return chunks


def _map_chunk(
    chunk: str,
    i: int,
    n: int,
    engine: str,
    cfg: dict,
    *,
    chat_model_key: str | None = None,
    errors: list[str] | None = None,
) -> str:
    """Extract raw material from one chunk. A single failed chunk returns ``""``
    (logged, its reason appended to ``errors``) rather than sinking the whole
    condensation."""
    try:
        return one_shot(
            MAP_SYSTEM,
            MAP_USER.format(i=i, n=n, chunk=chunk),
            max_tokens=int(cfg["map_max_tokens"]),
            engine=engine,
            # Only the "local" alias follows a local model key; resolve_local_key
            # ignores a cloud chat key, and a cloud or specific-key map ignores it.
            local_model_key=chat_model_key if engine == "local" else None,
            feature="Transcript condensation",
            source="condensation",
        )
    except Exception as e:
        log.warning("map extraction failed for chunk %d/%d: %s", i, n, e)
        if errors is not None:
            errors.append(str(e))
        return ""


def _hard_truncate(text: str, limit: int, reason: str) -> str:
    """The first ``limit`` characters, marked so the reading model knows the
    rest is missing and why (and can tell the user)."""
    return (
        text[:limit] + "\n\n[Transcript truncated here: it was too long to summarise in full, "
        f"and {reason}. Only the first {limit:,} characters are included; say so if the "
        "answer depends on the rest.]"
    )
