"""Attachment injection for the chat context — live turns and history replay.

Attachments are session state: the durable store (server/attachment_store.py)
keeps every file bound to its session, and the chat history persisted by the
frontend carries per-message ``attachmentIds``. This module renders both into
the model context:

  - ``render_attachment_blocks`` builds the text parts and image blocks for a
    set of attachment ids (shared by the live turn and history replay, so the
    rendering is deterministic and the reconstructed prompt prefix is stable
    across turns — which is what lets prompt caching absorb the repeat cost).
  - ``rebuild_history_message`` re-injects a historical user message's
    attachments at its original position, so a file attached three turns ago
    is still literally in the context now.
  - ``ensure_attachments_present`` is the safety net for the two paths that
    can evict an attach-turn message wholesale (compaction, the frontend's
    history cap): any session attachment whose content is no longer anywhere
    in the message list is re-injected as a leading user message.

A missing id (expired, GC'd, or foreign) is never skipped silently: the model
gets an explicit unavailability marker naming the file, so it can tell the
user instead of hallucinating around a gap.
"""

import logging

from server import attachment_store
from server.attachments import attachments as _hot_cache

log = logging.getLogger("whisper-studio")

# Per-file inline cap. markitdown turns a typical xlsx into several MB of
# markdown — a single such file alone blows past Bedrock's 200k-token input
# window, the model rejects the call, reactive compaction has nothing to
# compact, and the user sees "Conversation too long even after compaction" on
# what looks like the first turn. 150k chars (~37k tokens) is small enough to
# coexist with system prompt + tools + reply budget, large enough for any
# real-world spreadsheet header + first several thousand rows. The full text
# stays in the store for analyze_document section fetches.
MAX_ATTACHMENT_CHARS = 150_000


def resolve_attachment(aid: str) -> dict | None:
    """Hot in-memory cache first, then the durable store."""
    att = _hot_cache.get(aid)
    if att is not None:
        return att
    return attachment_store.get_attachment(aid)


def _missing_marker(name: str) -> str:
    return (
        f'[Attachment "{name}" is no longer available (expired or removed). '
        f"Tell the user to re-attach it if its content is needed.]"
    )


def render_attachment_blocks(
    attachment_ids: list[str],
    attachment_names: list[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """Render attachment ids into ``(text_parts, image_blocks)``.

    Every attachment yields at least one text part (documents their content,
    images a ``[Image: name]`` marker with any OCR text) so presence in a
    rebuilt context is always detectable by filename marker — that is what
    ``ensure_attachments_present`` keys on. Unresolvable ids yield an explicit
    unavailability marker instead of silence.
    """
    names = attachment_names or []
    attachment_texts: list[str] = []
    image_blocks: list[dict] = []
    for i, aid in enumerate(attachment_ids):
        att = resolve_attachment(aid)
        if not att:
            name = names[i] if i < len(names) else aid
            log.warning("attachment %s (%s) unresolvable; injecting marker", aid, name)
            attachment_texts.append(_missing_marker(name))
            continue
        if att["kind"] == "image":
            image_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att["media_type"],
                        "data": att["data"],
                    },
                }
            )
            # Text-only models (the local Gemma build) can't read the pixels,
            # so surface any OCR'd text as a sibling text part. Vision models
            # still get the image block above. The marker is emitted even with
            # no OCR text so the image's presence is detectable in the context.
            ocr_text = att.get("ocr_text")
            if ocr_text:
                attachment_texts.append(
                    f"[Image: {att['filename']} — transcribed text]\n{ocr_text}"
                )
            else:
                attachment_texts.append(f"[Image: {att['filename']}]")
        elif att["kind"] == "document":
            doc_text = att["text"]
            full_len = len(doc_text)
            if full_len > MAX_ATTACHMENT_CHARS:
                # Lead with the heading outline + the first slice, and point
                # the model at the analyze_document tool to pull specific
                # sections in full instead of blindly truncating the tail.
                head = doc_text[:MAX_ATTACHMENT_CHARS]
                outline = att.get("outline") or ""
                note = (
                    f"\n\n[Showing the first {MAX_ATTACHMENT_CHARS:,} of "
                    f"{full_len:,} characters. Call the analyze_document tool "
                    f"with section=<number from the outline> to read a specific "
                    f"section in full.]"
                )
                doc_text = f"[Outline]\n{outline}\n\n{head}{note}" if outline else head + note
            attachment_texts.append(f"[File: {att['filename']}]\n{doc_text}")
            # Video documents carry retained keyframes — surface them as image
            # blocks so vision models can see the actual frames (hybrid video
            # understanding), on top of the transcript + OCR text above. The
            # sampler already bounds this to <= 20 frames per video.
            for fr in att.get("frames", []):
                image_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": fr.get("media_type", "image/jpeg"),
                            "data": fr["data"],
                        },
                    }
                )
    return attachment_texts, image_blocks


def rebuild_history_message(msg: dict) -> dict:
    """A persisted history row → a prompt message, with any attachments the
    row references re-injected ahead of the typed text (the same layout the
    live attach turn produced)."""
    role = msg.get("role", "user")
    content = msg.get("content", "")
    ids = msg.get("attachmentIds") or []
    if role != "user" or not ids or not isinstance(content, str):
        return {"role": role, "content": content}
    texts, image_blocks = render_attachment_blocks(ids, msg.get("attachmentNames") or [])
    user_text = "\n\n".join([*texts, content]) if texts else content
    if image_blocks:
        return {
            "role": role,
            "content": image_blocks + [{"type": "text", "text": user_text}],
        }
    return {"role": role, "content": user_text}


def collect_history_attachment_ids(history: list[dict]) -> list[str]:
    """Every attachment id referenced by the persisted history, in order."""
    ids: list[str] = []
    for msg in history:
        for aid in msg.get("attachmentIds") or []:
            if aid not in ids:
                ids.append(aid)
    return ids


def _message_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def ensure_attachments_present(messages: list, session_id: str) -> list:
    """Re-inject any session attachment whose content is no longer in the
    context (its attach-turn message was compacted away or fell off the
    frontend's history cap) as one leading user message.

    Presence is detected by the filename markers ``render_attachment_blocks``
    always emits, so a rebuilt or live message already carrying the file is
    never duplicated.
    """
    if not session_id:
        return messages
    try:
        bound = attachment_store.load_session_attachments(session_id)
    except Exception:
        log.warning("session attachment lookup failed for %s", session_id, exc_info=True)
        return messages
    if not bound:
        return messages
    haystack = "\n".join(_message_text(m) for m in messages)
    missing = [
        aid
        for aid, att in bound.items()
        if f"[File: {att['filename']}]" not in haystack
        and f"[Image: {att['filename']}" not in haystack
        and _missing_marker(att["filename"]) not in haystack
    ]
    if not missing:
        return messages
    texts, image_blocks = render_attachment_blocks(missing)
    if not texts and not image_blocks:
        return messages
    log.info(
        "re-injecting %d session attachment(s) evicted from the context (session %s)",
        len(missing),
        session_id,
    )
    user_text = "\n\n".join(["[Files attached earlier in this session]", *texts])
    lead: dict = {"role": "user", "content": user_text}
    if image_blocks:
        lead = {
            "role": "user",
            "content": image_blocks + [{"type": "text", "text": user_text}],
        }
    return [lead] + messages
