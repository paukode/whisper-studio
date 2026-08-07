"""Attachment injection into the chat context (server/chat/attachment_context).

The bug this guards against: attachment content was injected only on the turn
the file was attached, never persisted, so the model literally lost the file
on the next exchange and asked the user to re-attach it. Now history rows
carry attachmentIds and the context rebuild re-injects each file at its
original position; compaction/history-cap eviction is caught by
ensure_attachments_present; a missing id yields a loud marker.
"""

import time
import uuid

from server import attachment_store
from server.attachments import attachments as hot_cache
from server.chat.attachment_context import (
    MAX_ATTACHMENT_CHARS,
    collect_history_attachment_ids,
    ensure_attachments_present,
    rebuild_history_message,
    render_attachment_blocks,
)


def _purge_session_attachments(session_id: str) -> None:
    """Test cleanup: drop the rows this test bound to its session (production
    session deletion does this inline in sessions._delete_session_sync)."""
    with attachment_store._get_conn() as conn:
        conn.execute("DELETE FROM attachments WHERE session_id = ?", (session_id,))


def _stash_doc(text="alpha beta gamma", filename="doc.md", outline=""):
    aid = str(uuid.uuid4())
    hot_cache[aid] = {
        "kind": "document",
        "filename": filename,
        "text": text,
        "outline": outline,
        "sections": [],
        "created": time.time(),
    }
    return aid


def _stash_image(filename="shot.png", ocr_text="ocr words"):
    aid = str(uuid.uuid4())
    hot_cache[aid] = {
        "kind": "image",
        "filename": filename,
        "media_type": "image/png",
        "data": "aW1n",
        "ocr_text": ocr_text,
        "created": time.time(),
    }
    return aid


def test_document_renders_inline():
    aid = _stash_doc()
    texts, images = render_attachment_blocks([aid])
    assert texts == ["[File: doc.md]\nalpha beta gamma"]
    assert images == []


def test_oversized_document_gets_outline_and_note():
    aid = _stash_doc(text="x" * (MAX_ATTACHMENT_CHARS + 10), outline="1. Intro")
    texts, _ = render_attachment_blocks([aid])
    assert len(texts) == 1
    assert texts[0].startswith("[File: doc.md]\n[Outline]\n1. Intro")
    assert "analyze_document" in texts[0]
    # The inline slice is capped; the tail is not shipped.
    assert len(texts[0]) < MAX_ATTACHMENT_CHARS + 500


def test_image_renders_block_and_marker():
    aid = _stash_image()
    texts, images = render_attachment_blocks([aid])
    assert images[0]["source"]["data"] == "aW1n"
    assert texts[0].startswith("[Image: shot.png")
    assert "ocr words" in texts[0]


def test_image_without_ocr_still_emits_marker():
    aid = _stash_image(ocr_text="")
    texts, images = render_attachment_blocks([aid])
    assert len(images) == 1
    assert texts == ["[Image: shot.png]"]


def test_missing_id_yields_loud_marker_with_name():
    texts, images = render_attachment_blocks(["gone-id"], ["report.pdf"])
    assert images == []
    assert len(texts) == 1
    assert '"report.pdf"' in texts[0]
    assert "no longer available" in texts[0]


def test_store_fallback_when_hot_cache_cold():
    # Simulates a backend restart: the durable row exists, the dict does not.
    aid = str(uuid.uuid4())
    attachment_store.save_attachment(
        aid,
        {
            "kind": "document",
            "filename": "survivor.md",
            "text": "still here",
            "created": time.time(),
        },
    )
    texts, _ = render_attachment_blocks([aid])
    assert texts == ["[File: survivor.md]\nstill here"]


def test_rebuild_history_reinjects_at_original_position():
    aid = _stash_doc(filename="spec.md", text="the spec body")
    msg = {
        "role": "user",
        "content": "summarize the spec",
        "attachmentIds": [aid],
        "attachmentNames": ["spec.md"],
    }
    out = rebuild_history_message(msg)
    assert out["role"] == "user"
    assert out["content"] == "[File: spec.md]\nthe spec body\n\nsummarize the spec"


def test_rebuild_history_with_image_produces_blocks():
    aid = _stash_image()
    out = rebuild_history_message(
        {"role": "user", "content": "what is in it?", "attachmentIds": [aid]}
    )
    assert isinstance(out["content"], list)
    assert out["content"][0]["type"] == "image"
    assert out["content"][-1]["type"] == "text"
    assert out["content"][-1]["text"].endswith("what is in it?")


def test_rebuild_history_passthrough_without_ids():
    for msg in (
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "reply", "attachmentIds": ["x"]},
        {"role": "user", "content": [{"type": "text", "text": "blocks"}]},
    ):
        assert rebuild_history_message(msg) == {
            "role": msg["role"],
            "content": msg["content"],
        }


def test_collect_history_ids_dedupes_in_order():
    history = [
        {"role": "user", "content": "a", "attachmentIds": ["one", "two"]},
        {"role": "assistant", "content": "r"},
        {"role": "user", "content": "b", "attachmentIds": ["two", "three"]},
    ]
    assert collect_history_attachment_ids(history) == ["one", "two", "three"]


def test_ensure_reinjects_evicted_session_attachment():
    sid = f"attctx-{uuid.uuid4()}"
    aid = _stash_doc(filename="evicted.md", text="lost content")
    attachment_store.save_attachment(aid, hot_cache[aid])
    attachment_store.bind_to_session([aid], sid)

    messages = [{"role": "user", "content": "unrelated turn"}]
    out = ensure_attachments_present(messages, sid)
    assert len(out) == 2
    assert "[Files attached earlier in this session]" in out[0]["content"]
    assert "[File: evicted.md]\nlost content" in out[0]["content"]
    assert out[1] == messages[0]
    _purge_session_attachments(sid)


def test_ensure_never_duplicates_present_attachment():
    sid = f"attctx-{uuid.uuid4()}"
    aid = _stash_doc(filename="present.md", text="body")
    attachment_store.save_attachment(aid, hot_cache[aid])
    attachment_store.bind_to_session([aid], sid)

    messages = [rebuild_history_message({"role": "user", "content": "q", "attachmentIds": [aid]})]
    assert ensure_attachments_present(messages, sid) == messages
    _purge_session_attachments(sid)


def test_ensure_detects_images_in_content_blocks():
    sid = f"attctx-{uuid.uuid4()}"
    aid = _stash_image(filename="pic.png", ocr_text="")
    attachment_store.save_attachment(aid, hot_cache[aid])
    attachment_store.bind_to_session([aid], sid)

    present = [rebuild_history_message({"role": "user", "content": "look", "attachmentIds": [aid]})]
    assert ensure_attachments_present(present, sid) == present

    evicted = [{"role": "user", "content": "later turn"}]
    out = ensure_attachments_present(evicted, sid)
    assert len(out) == 2
    assert out[0]["content"][0]["type"] == "image"
    _purge_session_attachments(sid)


def test_ensure_noop_without_session_or_bindings():
    messages = [{"role": "user", "content": "hi"}]
    assert ensure_attachments_present(messages, "") == messages
    assert ensure_attachments_present(messages, f"attctx-{uuid.uuid4()}") == messages
