"""Contextual chunk headers (optional, per-workspace setting; the LLM mode of
``chunk_context``).

Anthropic-style "contextual retrieval": before embedding a chunk, prepend a
short LLM-written line that situates it within its document, so a chunk that
loses its context when split out (which company? which file? which section?)
still embeds — and retrieves — with that context. Only the embedding input is
contextualized; the stored chunk text stays raw.

Same engine switch as ``relations.py`` / ``descriptions.py``: "haiku" (Bedrock,
cloud) or "local" (on-device Gemma). Best-effort — any failure yields an empty
context for that chunk and never breaks indexing (the chunk still embeds on its
own text).
"""

import logging

log = logging.getLogger("whisper-studio")

_SYSTEM = (
    "You situate a chunk within its document for search retrieval. Given the "
    "FILE name, a DOCUMENT excerpt, and one CHUNK, write a SINGLE short sentence "
    "(<=30 words) stating what the document is and what this chunk covers — "
    "include the key entities (who/what/which file). Output ONLY that sentence, "
    "no preamble, no quotes."
)

_DOC_HEAD_CHARS = 4000  # bound the per-call doc context (whole file for small docs)
# Keep the header short: prepended to the chunk, it shares the embedder's 512-token
# window, so an overlong header would truncate the tail of a near-max chunk.
_MAX_CONTEXT_CHARS = 200
_LOCAL_MODEL_KEY = "local_gemma"


def contextualize_chunks(
    filename: str, doc_text: str, chunk_texts: list[str], engine: str = "haiku"
) -> list[str]:
    """Return one situating context string per chunk (parallel to
    ``chunk_texts``); "" for a chunk the model couldn't contextualize. ``engine``
    is "haiku" (cloud) or "local" (on-device Gemma); anything else returns all
    empty (caller then embeds the bare chunk)."""
    if engine not in ("haiku", "local") or not chunk_texts:
        return [""] * len(chunk_texts)
    doc_head = (doc_text or "")[:_DOC_HEAD_CHARS]
    out: list[str] = []
    for ct in chunk_texts:
        ctx = _complete(_SYSTEM, _build_user(filename, doc_head, ct), engine)
        out.append(" ".join(ctx.split())[:_MAX_CONTEXT_CHARS])
    return out


def _build_user(filename: str, doc_head: str, chunk: str) -> str:
    return (
        f"FILE: {filename}\n\nDOCUMENT (excerpt):\n{doc_head}\n\n"
        f"CHUNK:\n{chunk[:2000]}\n\nContext sentence:"
    )


def _complete(system: str, user: str, engine: str) -> str:
    try:
        if engine == "local":
            from server.local import runtime as local_rt

            if not local_rt.is_downloaded(_LOCAL_MODEL_KEY):
                log.warning(
                    "chunk contextualization: %s not downloaded; skipping", _LOCAL_MODEL_KEY
                )
                return ""
            return local_rt.complete(_LOCAL_MODEL_KEY, system, user, max_tokens=120)
        import json

        from server.chat.infra import _get_bedrock_client, _get_chat_models

        model_id = _get_chat_models().get("haiku")
        if not model_id:
            return ""
        client = _get_bedrock_client()
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 120,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        )
        resp = client.invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        return "".join(
            b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
        )
    except Exception as e:  # noqa: BLE001 — contextualization is best-effort
        log.warning("chunk contextualization (%s) failed: %s", engine, e)
        return ""
