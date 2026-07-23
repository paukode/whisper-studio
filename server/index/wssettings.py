"""Per-workspace index settings: schedule, typed relations, background refresh.

Each indexed workspace stores its own settings in its index ``meta`` table, so
they travel with the index and are independent per folder. Values are validated
on read and write.

Shape::

    {
      "schedule": {"enabled": bool, "hour": 0-23, "frequency": "daily|every_n_days|weekly",
                   "interval_days": 1-30, "weekday": "mon".."sun"},
      "typed_relations": {"enabled": bool, "engine": "none|haiku|local|gliner2"},
      "refresh_when_closed": bool,
    }
"""

from __future__ import annotations

import copy
import json
import os

from . import paths, store

FREQUENCIES = ("daily", "every_n_days", "weekly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENGINES = ("none", "haiku", "local")
# Relations may additionally be extracted natively by GLiNER2 (no LLM). This is
# independent of ner_model: a workspace can use GLiNER2 for entities but keep an
# LLM for the richer relation set, or vice versa.
RELATION_ENGINES = ("none", "haiku", "local", "gliner2")
CONTEXT_MODES = ("off", "filename", "llm")  # chunk_context.mode
CONTEXT_ENGINES = ("haiku", "local")  # chunk_context.engine (llm mode only)
NER_MODELS = ("gliner", "gliner2")  # ner_model: which on-device NER model runs
MAX_INTERVAL_DAYS = 30

# Settings set BEFORE a folder is indexed have nowhere to live yet (the index
# meta DB doesn't exist), so they're held here, keyed by absolute path, and
# promoted into the index meta on first index. Lets the user configure a folder
# (schedule, relationships) before/while it indexes — relationships are then
# captured on the first pass instead of needing a reindex.
_PENDING_PATH = os.path.join(os.path.dirname(paths.INDEX_DATA_DIR), "index_pending_settings.json")

_DEFAULTS = {
    "schedule": {
        "enabled": False,
        "hour": 7,
        "frequency": "daily",
        "interval_days": 2,
        "weekday": "mon",
    },
    "typed_relations": {"enabled": False, "engine": "none"},
    "entity_descriptions": {"enabled": False, "engine": "none"},
    # Contextual chunk headers prepended before embedding (improves retrieval for
    # filename/section-relevant queries). "off" = embed content only; "filename" =
    # prepend the file path (free, offline); "llm" = an LLM writes a situating
    # line per chunk (engine: haiku=cloud, local=on-device Gemma).
    "chunk_context": {"mode": "filename", "engine": "local"},
    # Which on-device NER model runs for ENTITIES only (relations are chosen
    # separately via typed_relations.engine): "gliner" (default, gliner_large-v2.5,
    # strongest multilingual) or "gliner2" (fastino, English-strong, for
    # English-only corpora or when non-English quality is not a priority).
    # The entity label set (business vs code) is picked automatically per file.
    "ner_model": "gliner",
    "refresh_when_closed": False,
}


def _validated(raw) -> dict:
    out = copy.deepcopy(_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    sch = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    out["schedule"]["enabled"] = bool(sch.get("enabled", False))
    try:
        out["schedule"]["hour"] = max(0, min(int(sch.get("hour", 7)), 23))
    except (TypeError, ValueError):
        pass
    freq = str(sch.get("frequency", "daily")).lower()
    out["schedule"]["frequency"] = freq if freq in FREQUENCIES else "daily"
    try:
        out["schedule"]["interval_days"] = max(
            1, min(int(sch.get("interval_days", 2)), MAX_INTERVAL_DAYS)
        )
    except (TypeError, ValueError):
        pass
    wd = str(sch.get("weekday", "mon")).lower()
    out["schedule"]["weekday"] = wd if wd in WEEKDAYS else "mon"

    tr = raw.get("typed_relations") if isinstance(raw.get("typed_relations"), dict) else {}
    out["typed_relations"]["enabled"] = bool(tr.get("enabled", False))
    eng = str(tr.get("engine", "none")).lower()
    out["typed_relations"]["engine"] = eng if eng in RELATION_ENGINES else "none"

    ed = raw.get("entity_descriptions") if isinstance(raw.get("entity_descriptions"), dict) else {}
    out["entity_descriptions"]["enabled"] = bool(ed.get("enabled", False))
    eng_d = str(ed.get("engine", "none")).lower()
    out["entity_descriptions"]["engine"] = eng_d if eng_d in ENGINES else "none"

    cc = raw.get("chunk_context") if isinstance(raw.get("chunk_context"), dict) else {}
    mode = str(cc.get("mode", "filename")).lower()
    out["chunk_context"]["mode"] = mode if mode in CONTEXT_MODES else "filename"
    eng_c = str(cc.get("engine", "local")).lower()
    out["chunk_context"]["engine"] = eng_c if eng_c in CONTEXT_ENGINES else "local"

    nm = str(raw.get("ner_model", "gliner")).lower()
    out["ner_model"] = nm if nm in NER_MODELS else "gliner"

    out["refresh_when_closed"] = bool(raw.get("refresh_when_closed", False))
    return out


def _abs(ws_path: str) -> str:
    return os.path.abspath(os.path.expanduser(ws_path))


def _pending_all() -> dict:
    try:
        with open(_PENDING_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _pending_get(ws_path: str):
    return _pending_all().get(_abs(ws_path))


def _pending_set(ws_path: str, settings: dict) -> None:
    data = _pending_all()
    data[_abs(ws_path)] = settings
    os.makedirs(os.path.dirname(_PENDING_PATH), exist_ok=True)
    with open(_PENDING_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _pending_clear(ws_path: str) -> None:
    data = _pending_all()
    if data.pop(_abs(ws_path), None) is not None:
        with open(_PENDING_PATH, "w") as f:
            json.dump(data, f, indent=2)


def get_settings(ws_path: str) -> dict:
    """Validated settings for a folder. For a NOT-yet-indexed folder, read the
    pending store (or defaults) WITHOUT touching the index meta — reading meta
    would otherwise create an empty index DB (store._connect always creates it).
    For an indexed folder, read the meta; on first read seed it from any pending
    pre-index config, else defaults."""
    if not os.path.exists(store.db_path(ws_path)):
        pend = _pending_get(ws_path)
        return _validated(pend) if pend is not None else copy.deepcopy(_DEFAULTS)
    try:
        meta = store.get_meta(ws_path)
    except Exception:
        return copy.deepcopy(_DEFAULTS)
    raw = meta.get("settings")
    if raw is None:
        pend = _pending_get(ws_path)
        seeded = _validated(pend if pend is not None else _DEFAULTS)
        try:
            store.set_meta(ws_path, settings=seeded)
        except Exception:
            pass
        _pending_clear(ws_path)  # promote pre-index config into the index, then forget it
        return seeded
    return _validated(raw)


def update_settings(ws_path: str, patch: dict) -> dict:
    """Shallow-merge ``patch`` (by section) into a folder's settings, validate,
    and persist — to the index meta if indexed, else to the pending pre-index
    store (so configuring a folder before it's indexed never creates a fake
    empty index)."""
    cur = get_settings(ws_path)
    if isinstance(patch.get("schedule"), dict):
        cur["schedule"].update(patch["schedule"])
    if isinstance(patch.get("typed_relations"), dict):
        cur["typed_relations"].update(patch["typed_relations"])
    if isinstance(patch.get("entity_descriptions"), dict):
        cur["entity_descriptions"].update(patch["entity_descriptions"])
    if isinstance(patch.get("chunk_context"), dict):
        cur["chunk_context"].update(patch["chunk_context"])
    if "ner_model" in patch:
        cur["ner_model"] = patch["ner_model"]
    if "refresh_when_closed" in patch:
        cur["refresh_when_closed"] = bool(patch["refresh_when_closed"])
    validated = _validated(cur)
    if os.path.exists(store.db_path(ws_path)):
        store.set_meta(ws_path, settings=validated)
    else:
        _pending_set(ws_path, validated)
    return validated
