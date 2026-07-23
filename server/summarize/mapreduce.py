"""Map-reduce condensation for oversized transcripts.

Transcript-driven skills (meeting_notes, summarize_transcript, catch_up) inject
the whole transcript into the model's context so it can summarise it in one
pass. That is the best quality while the transcript fits the context window.
When it does not fit, the alternatives are to truncate it silently (a wrong
answer) or to overflow the request (a hard failure). Instead this module does
the "map" half of map-reduce: split the transcript into overlapping chunks,
extract the salient raw material from each with a fast one-shot model, and
concatenate the extracts. That condensed text is substituted in place of the
raw transcript, and the main chat model composes the requested summary from it
in its normal turn (the "reduce" half).

Only fires above a configurable size threshold; below it the transcript passes
through unchanged. It never returns an empty string: on any failure it falls
back to a hard-truncated transcript so the caller always has usable text.
"""

import hashlib
import logging
import threading
from collections import OrderedDict

from server.index.chunker import chunk_text
from server.infrastructure.config import load_config
from server.infrastructure.oneshot import one_shot, resolve_map_engine

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
    "threshold_chars": 600_000,  # ~150k tokens; above this, condense
    "chunk_chars": 150_000,  # ~37k tokens per chunk (cloud window)
    "local_chunk_chars": 40_000,  # ~10k tokens per chunk (fits the 16k local window)
    "overlap_chars": 800,
    "map_max_tokens": 1200,
    "max_chunks": 40,
    "max_output_chars": 480_000,  # cap on the condensed result (~120k tokens, cloud)
    "local_max_output_chars": 24_000,  # tighter cap so it fits a small local window
    "engine": "auto",  # "auto" -> follow model mode; or force "haiku" / "local"
}

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
    """The character length above which a transcript gets condensed."""
    return int(_cfg(config)["threshold_chars"])


def maybe_condense_transcript(
    text: str, *, config: dict | None = None, chat_model_key: str | None = None
) -> str:
    """Return ``text`` condensed to per-chunk extracts if it exceeds the size
    threshold, otherwise unchanged.

    ``chat_model_key`` names the active chat model; in local mode it is handed to
    the map step so condensation follows the resident model rather than evicting
    it (see :func:`server.infrastructure.oneshot._resolve_local_key`). It does
    not enter the cache key: the condensed text is identical whichever local
    model produced it, and the engine choice is already covered by the config
    signature.

    Never returns ``""``: on any failure it falls back to a hard-truncated
    transcript so the caller always has usable text to hand the model.
    """
    if not text or not text.strip():
        return text
    cfg = _cfg(config)
    if not cfg["enabled"] or len(text) <= int(cfg["threshold_chars"]):
        return text
    key = _cache_key(text, cfg)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
    if cached is not None:
        return cached
    # Run the (slow, blocking) map calls OUTSIDE the lock so concurrent requests
    # do not serialise; a duplicate miss just recomputes the same result.
    try:
        result = _condense(text, cfg, chat_model_key=chat_model_key)
    except Exception as e:
        log.warning("transcript condensation failed (%s); truncating instead", e)
        result = _hard_truncate(text, int(cfg["threshold_chars"]))
    with _lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


def _cache_key(text: str, cfg: dict) -> str:
    # Every field that changes the condensed output must be in the signature, or
    # a mid-run config change would keep returning the stale cached result.
    sig = ":".join(str(cfg[k]) for k in (*_INT_FIELDS, "engine"))
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return f"{digest}|{sig}"


def _condense(text: str, cfg: dict, *, chat_model_key: str | None = None) -> str:
    engine = _engine(cfg)
    chunk_chars = cfg["local_chunk_chars"] if engine == "local" else cfg["chunk_chars"]
    chunks = _chunk(text, chunk_chars, cfg["overlap_chars"], cfg["max_chunks"])
    if not chunks:
        return _hard_truncate(text, cfg["threshold_chars"])
    n = len(chunks)
    results = _map_all(chunks, engine, cfg, chat_model_key=chat_model_key)
    extracts = [r.strip() for r in results if r.strip()]
    if not extracts:
        # Every map call failed or came back empty; better to truncate the raw
        # transcript than to hand the model nothing.
        raise RuntimeError("all map extractions were empty")
    condensed = "\n\n".join(extracts)
    # Bound the condensed result to the target chat window (much tighter for a
    # local model) so the "reduce" turn does not overflow. The map input is
    # already window-sized; this caps the aggregate output.
    max_out = cfg["local_max_output_chars"] if engine == "local" else cfg["max_output_chars"]
    if len(condensed) > max_out:
        log.warning(
            "condensed extracts (%d chars) exceed max_output_chars %d (engine=%s); truncating",
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
    return NOTE_PREFIX + condensed


def _map_all(
    chunks: list[str], engine: str, cfg: dict, *, chat_model_key: str | None = None
) -> list[str]:
    """Run the per-chunk map calls, preserving order. The chunks are independent
    by construction (each extraction prompt is self-contained), so the cloud
    path fans them out with bounded concurrency to cut first-token latency. The
    local path stays serial: a single llama.cpp instance cannot run concurrent
    completions."""
    n = len(chunks)
    if engine == "local" or n == 1:
        return [
            _map_chunk(c, i, n, engine, cfg, chat_model_key=chat_model_key)
            for i, c in enumerate(chunks, 1)
        ]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(8, n)) as pool:
        # map() preserves input order, so the concatenation order stays stable.
        return list(
            pool.map(
                lambda it: _map_chunk(it[1], it[0], n, engine, cfg, chat_model_key=chat_model_key),
                enumerate(chunks, 1),
            )
        )


def _engine(cfg: dict) -> str:
    choice = str(cfg.get("engine") or "auto").strip()
    if choice in ("haiku", "local"):
        return choice
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
    chunk: str, i: int, n: int, engine: str, cfg: dict, *, chat_model_key: str | None = None
) -> str:
    """Extract raw material from one chunk. A single failed chunk returns ``""``
    (logged) rather than sinking the whole condensation."""
    try:
        return one_shot(
            MAP_SYSTEM,
            MAP_USER.format(i=i, n=n, chunk=chunk),
            max_tokens=int(cfg["map_max_tokens"]),
            engine=engine,
            # Only a local map uses a local model key; a cloud map ignores it. The
            # chat key is a Claude key in cloud mode, which is not a local model.
            local_model_key=chat_model_key if engine == "local" else None,
        )
    except Exception as e:
        log.warning("map extraction failed for chunk %d/%d: %s", i, n, e)
        return ""


def _hard_truncate(text: str, limit: int) -> str:
    return text[:limit] + "\n\n[Transcript truncated here: it was too long to summarise in full.]"
