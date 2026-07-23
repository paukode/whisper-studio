"""Typed relationship extraction (optional, per-workspace setting).

For each changed file, ask an LLM to extract typed entity↔entity relations
(works_at, cites, depends_on, …) from the file text given its already-extracted
GLiNER entities. This (local) build can use EITHER engine, chosen per workspace
in that folder's ⋯ menu and passed to ``extract_relations(..., engine=...)``:
  - "haiku" — Bedrock Claude Haiku (cloud, faster; needs AWS creds).
  - "local" — the on-device Gemma model (private, runs offline; slower).
Endpoints are validated against the known entity list so the LLM can't invent
nodes, and anything that fails returns [] so indexing never breaks.
"""

import json
import logging
import re

from .config import TYPED_RELATIONS_MAX_CHARS, TYPED_RELATIONS_MAX_ENTITIES
from .relations_vocab import canonicalize_predicate, prompt_block

log = logging.getLogger("whisper-studio")

_SYSTEM = (
    "From the TEXT, extract typed relationships between the listed ENTITIES. "
    'Return ONLY a JSON array of objects {"source": str, "target": str, '
    '"type": str, "strength": int}. source and target must be EXACTLY two of the '
    "listed entities. type MUST be one of these predicates (source -> target "
    "direction as described); choose the closest one and always use the canonical "
    "direction:\n" + prompt_block() + "\n"
    "If A employs B, output works_at with source B and target A. If A manages B, "
    "output reports_to with source B and target A. Use related_to ONLY when a "
    "clearly stated connection fits none of the others. strength is your confidence "
    "from 1 (weak/implied) to 5 (explicitly stated). The TEXT may be in Polish; keep "
    "entity spellings as listed but always use the English predicates above. "
    "Include only relationships clearly stated in the text. No prose, no markdown fence."
)


# Default on-device model used when the "local" engine is selected. Gemma 4 12B
# (the general instruct model), not the coder variant.
_LOCAL_MODEL_KEY = "local_gemma"


def extract_relations(
    text: str, entity_names: list[str], engine: str = "haiku"
) -> list[tuple[str, str, str, float]]:
    """Return ``[(source, target, type, score)]`` with endpoints drawn from
    ``entity_names`` (canonical spelling) and ``score`` the 1–5 strength. Empty on
    any failure or when there are fewer than two entities. ``engine`` is the
    per-workspace choice ("haiku" cloud / "local" on-device Gemma); anything else
    extracts nothing."""
    names = [n for n in dict.fromkeys(entity_names) if n][:TYPED_RELATIONS_MAX_ENTITIES]
    if len(names) < 2 or not (text or "").strip():
        return []
    user = f"ENTITIES:\n{', '.join(names)}\n\nTEXT:\n{text[:TYPED_RELATIONS_MAX_CHARS]}"
    if engine == "local":
        return _extract_via_local(user, names)
    if engine == "haiku":
        return _extract_via_haiku(user, names)
    return []  # "none" / unknown — nothing to extract


def _extract_via_haiku(user: str, names: list[str]) -> list[tuple[str, str, str]]:
    """Cloud path: Bedrock Claude Haiku."""
    try:
        from server.chat.infra import _get_bedrock_client, _get_chat_models

        model_id = _get_chat_models().get("haiku")
        if not model_id:
            return []
        client = _get_bedrock_client()
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1500,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": user}],
            }
        )
        resp = client.invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        out = "".join(
            b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
        )
        return parse_relations(out, names)
    except Exception as e:
        log.warning("typed-relation extraction (haiku) failed: %s", e)
        return []


def _extract_via_local(user: str, names: list[str]) -> list[tuple[str, str, str]]:
    """On-device path: the local Gemma model via llama.cpp. Generation is
    serialised on the single model executor (shared with chat), so this also
    makes indexing noticeably slower than the cloud path — the trade for
    keeping file contents on the device."""
    try:
        from server.local import runtime as local_rt

        if not local_rt.is_downloaded(_LOCAL_MODEL_KEY):
            log.warning(
                "typed-relation local engine: %s not downloaded; skipping relations",
                _LOCAL_MODEL_KEY,
            )
            return []
        out = local_rt.complete(_LOCAL_MODEL_KEY, _SYSTEM, user, max_tokens=1500)
        return parse_relations(out, names)
    except Exception as e:
        log.warning("typed-relation extraction (local) failed: %s", e)
        return []


def parse_relations(out_text: str, entity_names: list[str]) -> list[tuple[str, str, str, float]]:
    """Parse the LLM's JSON array into ``(source, target, type, score)`` tuples.

    Keeps only relations whose endpoints match a known entity (case-insensitively,
    mapped back to canonical spelling) AND whose predicate maps onto the closed
    vocabulary — an exact predicate is kept, a known synonym is remapped (swapping
    source/target when it inverts the relation), and anything else is dropped
    rather than kept as noise. ``score`` is the 1–5 strength (default 3.0)."""
    m = re.search(r"\[.*\]", out_text or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group())
    except Exception:
        return []
    by_lower = {n.lower(): n for n in entity_names}
    rels: list[tuple[str, str, str, float]] = []
    seen = set()
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict):
            continue
        s = by_lower.get(str(item.get("source", "")).strip().lower())
        t = by_lower.get(str(item.get("target", "")).strip().lower())
        mapped = canonicalize_predicate(str(item.get("type", "")))
        try:
            score = max(1.0, min(float(item.get("strength", 3)), 5.0))
        except (TypeError, ValueError):
            score = 3.0
        if not (s and t and mapped) or s == t:
            continue
        ty, swap = mapped
        if swap:
            s, t = t, s
        if (s, t, ty) not in seen:
            seen.add((s, t, ty))
            rels.append((s, t, ty, score))
    return rels
