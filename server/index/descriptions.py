"""Per-entity descriptions (optional, per-workspace setting).

After dedup, ask an LLM to write a one-sentence description for each canonical
entity from the chunks it appears in. This turns bare ``(name, label)`` nodes
into disambiguating summaries — the single biggest "meaningfulness" upgrade over
NER + co-occurrence (it distinguishes two functions both named ``process`` and
gives the entity-pivot view real content). Runs PER CANONICAL ENTITY (thousands
of entities, not per chunk), so the token cost is a fraction of full LLM
extraction, and it keeps GLiNER as the fast, hallucination-free NER backbone.

Same engine switch as ``relations.py``: "haiku" (Bedrock, cloud) or "local"
(on-device Gemma). Best-effort — any failure yields no descriptions and never
breaks indexing.
"""

import json
import logging
import re

log = logging.getLogger("whisper-studio")

_SYSTEM = (
    "For each ENTITY below, write a concise one-sentence description grounded "
    "ONLY in the provided context. Return ONLY a JSON array of objects "
    '{"name": str, "label": str, "description": str}, one per input entity, '
    "preserving each name and label EXACTLY. If the context is insufficient, give "
    "a short generic description of what the entity is. No prose, no markdown fence."
)

# Entities are described in small batches so it's a handful of LLM calls, not one
# per entity. Each batch returns a JSON array mapping back to its inputs.
_BATCH = 12

_LOCAL_MODEL_KEY = "local_gemma"


def describe_entities(items: list[dict], engine: str = "haiku") -> list[dict]:
    """``items`` is ``[{name, label, contexts: [str]}]``. Returns
    ``[{name, label, description}]`` for the entities the model described.
    ``engine`` is "haiku" (cloud) or "local" (on-device Gemma); anything else
    returns nothing."""
    if engine not in ("haiku", "local") or not items:
        return []
    out: list[dict] = []
    for i in range(0, len(items), _BATCH):
        batch = items[i : i + _BATCH]
        raw = _complete(_SYSTEM, _build_user(batch), engine)
        out.extend(_parse(raw, batch))
    return out


def _build_user(batch: list[dict]) -> str:
    lines = []
    for e in batch:
        ctx = " ".join((c or "").replace("\n", " ") for c in e.get("contexts", []))[:600]
        lines.append(f"- name: {e['name']} | label: {e['label']} | context: {ctx}")
    return "ENTITIES:\n" + "\n".join(lines)


def _complete(system: str, user: str, engine: str) -> str:
    try:
        if engine == "local":
            from server.local import runtime as local_rt

            if not local_rt.is_downloaded(_LOCAL_MODEL_KEY):
                log.warning("entity descriptions: %s not downloaded; skipping", _LOCAL_MODEL_KEY)
                return ""
            return local_rt.complete(_LOCAL_MODEL_KEY, system, user, max_tokens=1200)
        from server.chat.infra import _get_bedrock_client, _get_chat_models

        model_id = _get_chat_models().get("haiku")
        if not model_id:
            return ""
        client = _get_bedrock_client()
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1200,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        )
        resp = client.invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        return "".join(
            b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
        )
    except Exception as e:  # noqa: BLE001 — descriptions are best-effort
        log.warning("entity description (%s) failed: %s", engine, e)
        return ""


def _parse(out_text: str, batch: list[dict]) -> list[dict]:
    """Parse the JSON array, keeping only descriptions whose (name, label) match
    an input entity (mapped back to its exact spelling)."""
    m = re.search(r"\[.*\]", out_text or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group())
    except Exception:
        return []
    by_lower = {(e["name"].lower(), e["label"].lower()): (e["name"], e["label"]) for e in batch}
    out: list[dict] = []
    seen = set()
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name", "")).strip()
        lb = str(it.get("label", "")).strip()
        d = str(it.get("description", "")).strip()
        key = by_lower.get((nm.lower(), lb.lower()))
        if key and d and key not in seen:
            seen.add(key)
            out.append({"name": key[0], "label": key[1], "description": d})
    return out
