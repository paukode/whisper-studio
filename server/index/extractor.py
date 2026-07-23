"""GLiNER entity extraction — chunk text → knowledge-graph nodes/edges.

Zero-shot NER: GLiNER is asked for the entity types in ``ENTITY_LABELS`` and
returns spans it believes match. Each distinct entity becomes a graph node;
entities that co-occur in the same chunk get an edge (weight = co-occurrence
count). That's the lightweight, fully-local GraphRAG graph for v1 — no LLM call
per chunk. Lazy-loaded and ``unload()``-able like the embedder.
"""

from __future__ import annotations

import logging
import threading
import warnings

from .config import (
    GLINER2_MODEL,
    GLINER2_MODEL_DIR,
    GLINER2_RELATION_MIN_CONF,
    GLINER2_SENTINEL,
    GLINER_CALL_THRESHOLD,
    GLINER_MODEL,
    GLINER_MODEL_DIR,
    GLINER_SENTINEL,
    LABEL_THRESHOLDS,
    labels_for_profile,
)
from .relations_vocab import PREDICATES, canonicalize_predicate
from .salience import is_hard_junk

log = logging.getLogger("whisper-studio")

_model = None  # GLiNER (gliner_large-v2.5)
_gliner2_model = None  # GLiNER2 (fastino) — optional per-workspace alternative
_lock = threading.Lock()

# GLiNER's transformer backbone caps input length; keep well under it per call.
# A chunk runs to ~1600 chars, so a single window would never see its tail — we
# scan two overlapping windows and union the spans, bounded by _MAX_WINDOWS.
_MAX_CHARS = 1200
_WINDOW_OVERLAP = 400
_MAX_WINDOWS = 6  # cap GLiNER calls per chunk (guards pathological long-line chunks)
# Cloud NER sends the whole chunk in one call (Claude handles far more than
# _MAX_CHARS), covering the same span as GLiNER's windows without extra calls.
_HAIKU_MAX_CHARS = 6000


def _accept(name: str, label: str, score: float) -> bool:
    """Keep a span only if it clears its label's confidence floor and isn't
    unambiguous junk (code identifiers, generic words, label echoes …)."""
    if not name or is_hard_junk(name, label):
        return False
    floor = LABEL_THRESHOLDS.get(label, LABEL_THRESHOLDS.get("default", 0.55))
    return score >= floor


def _ner_backend() -> str:
    try:
        from server.infrastructure.model_mode import resolve_backend

        return resolve_backend("ner")
    except Exception:
        return "gliner"


def _extract_via_haiku(snippet: str, labels: list[str]) -> list[dict]:
    """Cloud NER: ask Bedrock Claude Haiku for entities, constrained to the active
    profile's label set so graph node labels stay consistent across backends.
    Best-effort: returns [] on any failure so a build never breaks."""
    import json

    try:
        from server.chat.infra import _get_bedrock_client, _get_chat_models

        model_id = _get_chat_models().get("haiku")
        if not model_id:
            return []
        label_str = ", ".join(labels)
        system = (
            "You extract named entities from a text snippet for a knowledge graph. "
            f"Use ONLY these entity types (verbatim): {label_str}. "
            'Return ONLY a compact JSON array of objects {"name": ..., "label": ...}, '
            "no prose, no code fences. Skip anything that doesn't fit a listed type. "
            "Names must be short noun phrases copied from the text."
        )
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "system": system,
                "messages": [{"role": "user", "content": snippet}],
            }
        )
        resp = _get_bedrock_client().invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        out_text = "".join(
            b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
        ).strip()
        if out_text.startswith("```"):
            out_text = out_text.strip("`")
            out_text = out_text[out_text.find("[") : out_text.rfind("]") + 1]
        items = json.loads(out_text) if out_text else []
    except Exception as e:  # noqa: BLE001 — NER is best-effort
        log.debug("Haiku NER failed on a chunk: %s", e)
        return []

    # Canonicalize against the ACTIVE profile's labels (not the global union), so a
    # label outside this workspace's profile is dropped — matching GLiNER's hard
    # label constraint. Casing (e.g. "api" -> "API") is still normalized.
    canon = {lbl.lower(): lbl for lbl in labels}
    seen: set[str] = set()
    result: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        label = canon.get(str(it.get("label") or "").strip().lower())
        # Haiku returns no per-span confidence; synthesize a high one so the
        # per-label floor passes and only the hard-junk gate in _accept applies.
        if not name or not label or not _accept(name, label, 0.8):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "label": label, "score": 0.8})
    return result


def ensure_gliner_model() -> str:
    """Download the GLiNER model into ./models if absent (idempotent).

    GLiNER loads its transformer backbone's tokenizer/config by HF repo id (the
    snapshot bundles the backbone *weights* but not the tokenizer), so we also
    pre-cache those small files on first download. That lets the model load
    fully offline afterwards (``local_files_only=True`` in ``_load``) instead of
    hitting the Hub for a cache-freshness check on every single load.
    """
    import json
    import os

    if not os.path.exists(GLINER_SENTINEL):
        from huggingface_hub import snapshot_download

        log.info("Downloading GLiNER model %s ...", GLINER_MODEL)
        snapshot_download(
            repo_id=GLINER_MODEL, local_dir=GLINER_MODEL_DIR, local_dir_use_symlinks=False
        )
        # Pre-cache the backbone tokenizer/config (not its weights) so the load
        # is offline-clean. Best-effort — _load falls back to an online fetch if
        # this is skipped or the backbone changes.
        try:
            from huggingface_hub import snapshot_download as _snap

            backbone = json.load(open(GLINER_SENTINEL)).get("model_name")
            if backbone:
                log.info("Caching GLiNER backbone tokenizer %s ...", backbone)
                _snap(repo_id=backbone, allow_patterns=["*.json", "*.txt", "*.model", "tokenizer*"])
        except Exception as e:  # noqa: BLE001 — best-effort pre-cache
            log.debug("GLiNER backbone pre-cache skipped: %s", e)
        log.info("GLiNER model download complete.")
    return GLINER_MODEL_DIR


_tokenizer_warning_quieted = False


def _quiet_spurious_tokenizer_warning() -> None:
    """Drop ONE spurious transformers log line, leaving every other warning visible.

    transformers 5.x warns that GLiNER's deberta-v3 backbone tokenizer has an
    "incorrect regex pattern" and to set ``fix_mistral_regex=True``. Verified
    empirically that this is a FALSE POSITIVE for deberta-v3: the default output
    is the correct SentencePiece tokenization (``▁John ▁Doe ▁2024``), and the
    suggested flag actually corrupts it (splits digits per character, emits
    whitespace tokens). So we suppress only this one message — not the tokenizer
    (which is correct) and not transformers warnings in general.
    """
    global _tokenizer_warning_quieted
    if _tokenizer_warning_quieted:
        return
    try:
        import transformers

        transformers.logging.get_verbosity()  # ensure the library logger + handler exist

        class _DropRegexFalsePositive(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return "fix_mistral_regex" not in record.getMessage()

        f = _DropRegexFalsePositive()
        tlog = logging.getLogger("transformers")
        tlog.addFilter(f)  # records emitted directly on the library root
        for h in tlog.handlers:  # child-logger records are filtered at the handler
            h.addFilter(f)
        _tokenizer_warning_quieted = True
    except Exception:  # noqa: BLE001 — a logging tweak must never block indexing
        pass


def _load():
    global _model
    if _model is not None:
        return
    from gliner import GLiNER

    _quiet_spurious_tokenizer_warning()
    ensure_gliner_model()
    log.info("Loading GLiNER model ...")
    # GLiNER loads its backbone tokenizer/config by HF repo id, and transformers
    # makes a cache-freshness HEAD call to the Hub for those on every load even
    # when they're cached — slow, rate-limited (unauthenticated), and broken
    # offline. `local_files_only` isn't plumbed through to the backbone config
    # load, so force Hub-offline for the duration of this load instead (scoped:
    # it does not affect other models' loads). Restored in `finally`.
    import huggingface_hub.constants as _hfc

    _prev_offline = _hfc.HF_HUB_OFFLINE
    try:
        _hfc.HF_HUB_OFFLINE = True
        _model = GLiNER.from_pretrained(GLINER_MODEL_DIR)
    except Exception as e:  # noqa: BLE001 — cache miss (first run): fetch once online
        _hfc.HF_HUB_OFFLINE = _prev_offline
        log.info("GLiNER backbone not cached (%s); fetching from the Hub once.", e)
        _model = GLiNER.from_pretrained(GLINER_MODEL_DIR)
    finally:
        _hfc.HF_HUB_OFFLINE = _prev_offline
    _model.eval()
    log.info("GLiNER model loaded.")


def ensure_gliner2_model() -> str:
    """Download the GLiNER2 model into ./models if absent (idempotent). Mirrors
    ensure_gliner_model so setup.sh can pre-pull it like the other weights."""
    import os

    if not os.path.exists(GLINER2_SENTINEL):
        from huggingface_hub import snapshot_download

        log.info("Downloading GLiNER2 model %s ...", GLINER2_MODEL)
        snapshot_download(
            repo_id=GLINER2_MODEL, local_dir=GLINER2_MODEL_DIR, local_dir_use_symlinks=False
        )
        log.info("GLiNER2 model download complete.")
    return GLINER2_MODEL_DIR


def _load_gliner2():
    global _gliner2_model
    if _gliner2_model is not None:
        return
    from gliner2 import GLiNER2

    ensure_gliner2_model()
    log.info("Loading GLiNER2 model ...")
    _gliner2_model = GLiNER2.from_pretrained(GLINER2_MODEL_DIR)
    log.info("GLiNER2 model loaded.")


def unload() -> None:
    global _model, _gliner2_model
    with _lock:
        if _model is None and _gliner2_model is None:
            return
        _model = None
        _gliner2_model = None
        import gc

        gc.collect()
        log.info("NER model(s) unloaded.")


def is_loaded() -> bool:
    return _model is not None or _gliner2_model is not None


def _windows(text: str) -> list[str]:
    """Overlapping GLiNER-sized windows covering the chunk (its tail would otherwise
    never be scanned, since a chunk is ~1600 chars and _MAX_CHARS=1200). Capped at
    _MAX_WINDOWS so a pathological single-line chunk (minified JS, a giant table
    row) can't trigger hundreds of GLiNER inferences and stall the build."""
    if len(text) <= _MAX_CHARS:
        return [text]
    step = _MAX_CHARS - _WINDOW_OVERLAP
    wins = [text[i : i + _MAX_CHARS] for i in range(0, len(text), step) if text[i : i + _MAX_CHARS]]
    return wins[:_MAX_WINDOWS]


def _extract_via_gliner2(text: str, labels: list[str]) -> list[dict]:
    """On-device NER via GLiNER2 (fastino). Same windowing + accept + dedup contract
    as the GLiNER path, adapting GLiNER2's grouped return
    ``{entities: {label: [{text, confidence}]}}`` into ``[{name, label, score}]``."""
    best: dict[str, dict] = {}
    with _lock:
        _load_gliner2()
        for window in _windows(text):
            if not window.strip():
                continue
            try:
                res = _gliner2_model.extract_entities(window, labels, include_confidence=True)
            except Exception as e:  # noqa: BLE001 — NER is best-effort
                log.debug("GLiNER2 extract failed on a chunk: %s", e)
                continue
            for label, items in (res.get("entities") or {}).items():
                for it in items or []:
                    name = str(it.get("text") or "").strip()
                    score = float(it.get("confidence") or 0.0)
                    if not _accept(name, label, score):
                        continue
                    key = name.lower()
                    if key not in best or score > best[key]["score"]:
                        best[key] = {"name": name, "label": label, "score": round(score, 4)}
    return list(best.values())


def extract_entities(
    text: str,
    labels: list[str] | None = None,
    backend: str | None = None,
    ner_model: str = "gliner",
) -> list[dict]:
    """Return distinct entities in ``text`` as ``[{name, label, score}]``.

    Routes to the active NER backend (on-device GLiNER/GLiNER2, or Claude Haiku on
    Bedrock in cloud mode). ``labels`` is the workspace's entity profile (defaults
    to the business set); ``ner_model`` picks the on-device family ("gliner" default,
    or "gliner2"). Spans are kept only if they clear their label's confidence floor
    and pass the hard-junk gate; ``score`` is the model's span confidence (used
    downstream to weight salience). Names are deduped case-insensitively, keeping
    the highest-scoring occurrence, so one entity maps to one graph node.
    """
    if not text.strip():
        return []
    labels = labels or labels_for_profile(None)
    if (backend or _ner_backend()) == "haiku":
        return _extract_via_haiku(text[:_HAIKU_MAX_CHARS], labels)
    if ner_model == "gliner2":
        return _extract_via_gliner2(text, labels)
    best: dict[str, dict] = {}  # name.lower() -> best-scoring accepted span
    with _lock:
        _load()
        for window in _windows(text):
            if not window.strip():
                continue
            try:
                # GLiNER truncates inputs over its token max and warns once per
                # chunk; that expected, harmless warning would flood a build.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message=r".*has been truncated.*", category=UserWarning
                    )
                    spans = _model.predict_entities(window, labels, threshold=GLINER_CALL_THRESHOLD)
            except Exception as e:
                log.debug("GLiNER extract failed on a chunk: %s", e)
                continue
            for s in spans:
                name = (s.get("text") or "").strip()
                label = (s.get("label") or "").strip()
                score = float(s.get("score") or 0.0)
                if not _accept(name, label, score):
                    continue
                key = name.lower()
                if key not in best or score > best[key]["score"]:
                    best[key] = {"name": name, "label": label, "score": round(score, 4)}
    return list(best.values())


def extract_relations_gliner2(
    text: str, entity_names: list[str], labels: list[str] | None = None
) -> list[tuple[str, str, str, float]]:
    """Native GLiNER2 relation extraction — the local, LLM-free replacement for the
    per-file LLM relations pass. One schema pass (entities + the closed predicate
    vocabulary) per window; returns ``[(source, target, predicate, score)]`` with
    endpoints mapped to KNOWN entity names, predicates snapped to the vocabulary,
    and ``score`` on the same 1–5 scale the LLM path emits (from GLiNER2's head/tail
    confidence). Empty on any failure or fewer than two entities."""
    names = [n for n in dict.fromkeys(entity_names) if n]
    if len(names) < 2 or not (text or "").strip():
        return []
    by_lower = {n.lower(): n for n in names}
    # Pass predicates in a readable form; canonicalize the returned label back.
    rel_labels = [p.replace("_", " ") for p in PREDICATES]
    labels = labels or labels_for_profile(None)
    # GLiNER2 over-generates: it scores many predicates for the same pair. Keep one
    # best-scoring predicate per directed (source, target) pair, above a confidence
    # floor, so the graph gets clean single edges instead of a fan of noisy guesses.
    best: dict[tuple, tuple] = {}  # (s, t) -> (predicate, score)
    with _lock:
        _load_gliner2()
        try:
            schema = _gliner2_model.create_schema().entities(labels).relations(rel_labels)
        except Exception as e:  # noqa: BLE001 — best-effort; never break a build
            log.warning("GLiNER2 relation schema build failed: %s", e)
            return []
        for window in _windows(text):
            if not window.strip():
                continue
            try:
                res = _gliner2_model.extract(window, schema, include_confidence=True)
            except Exception as e:  # noqa: BLE001
                log.debug("GLiNER2 relations failed on a window: %s", e)
                continue
            for pred, items in (res.get("relation_extraction") or {}).items():
                mapped = canonicalize_predicate(pred)
                if not mapped:
                    continue
                ptype, swap = mapped
                for it in items or []:
                    head = str((it.get("head") or {}).get("text") or "").strip()
                    tail = str((it.get("tail") or {}).get("text") or "").strip()
                    s = by_lower.get(head.lower())
                    t = by_lower.get(tail.lower())
                    if not (s and t) or s == t:
                        continue
                    if swap:
                        s, t = t, s
                    conf = min(
                        float((it.get("head") or {}).get("confidence") or 0.0),
                        float((it.get("tail") or {}).get("confidence") or 0.0),
                    )
                    if conf < GLINER2_RELATION_MIN_CONF:
                        continue
                    score = round(1.0 + 4.0 * conf, 2)  # map 0–1 confidence to the 1–5 scale
                    key = (s, t)
                    if key not in best or score > best[key][1]:
                        best[key] = (ptype, score)
    return [(s, t, p, sc) for (s, t), (p, sc) in best.items()]
