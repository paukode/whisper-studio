"""Image OCR and Local mode: the Haiku second reader runs only outside Local
mode. In Local mode an image Apple Vision cannot read stays on this Mac, and
the chat context says so while the mode lasts, instead of the image arriving
silently empty. The note is never stored as the image's text."""

from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.attachments as A
import server.extract.ocr as ocr
from server.infrastructure.model_mode import MODES


@pytest.fixture
def set_mode(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str) -> None:
        cfg = {**real, "model_mode": mode}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)

    return _set


@pytest.fixture
def cloud_reader(monkeypatch):
    """Credentials present; record every credential lookup and Haiku call."""
    seen: list[str] = []

    def creds():
        seen.append("creds")
        return True

    def haiku(images):
        seen.append("haiku")
        return "haiku-text"

    monkeypatch.setattr(ocr, "_aws_available", creds)
    monkeypatch.setattr(ocr, "_ocr_with_haiku", haiku)
    return seen


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("vision", ["empty", "fails"])
def test_haiku_reads_only_outside_local_mode(set_mode, cloud_reader, monkeypatch, mode, vision):
    set_mode(mode)

    def apple_vision(images):
        if vision == "fails":
            raise RuntimeError("Vision unavailable")
        return ""

    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", apple_vision)
    out = ocr.ocr_images(["img"])
    if mode == "local":
        assert out == "" and cloud_reader == []  # not even a credential lookup
    else:
        assert out == "haiku-text" and "haiku" in cloud_reader


def test_note_exists_exactly_in_local_mode(set_mode):
    for mode in MODES:
        set_mode(mode)
        note = ocr.local_mode_note()
        assert bool(note) is (mode == "local")
        if note:
            assert "Local mode" in note


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_local_mode_image_attachment_says_why_it_has_no_text(set_mode, cloud_reader, monkeypatch):
    from server import attachment_store
    from server.chat.attachment_context import render_attachment_blocks

    set_mode("local")
    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", lambda images: "")
    app = FastAPI()
    app.include_router(A.router)
    r = TestClient(app).post("/api/upload", files={"files": ("photo.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    aid = r.json()["attachments"][0]["id"]
    assert cloud_reader == []
    # Nothing was read, and nothing pretends to be the image's text: not the
    # hot record, not the durable row that session search indexes.
    assert A.attachments[aid]["ocr_text"] == ""
    assert attachment_store.get_attachment(aid)["ocr_text"] == ""

    texts, images = render_attachment_blocks([aid])
    assert images and texts == [f"[Image: photo.png]\n{ocr.local_mode_note()}"]
    assert "transcribed text" not in texts[0]
    # The note belongs to the mode, not the image: gone once cloud is allowed.
    set_mode("hybrid")
    assert render_attachment_blocks([aid])[0] == ["[Image: photo.png]"]


@pytest.mark.parametrize("mode", MODES)
def test_scanned_pdf_pages_reach_haiku_only_outside_local_mode(
    set_mode, cloud_reader, monkeypatch, mode
):
    """A scanned PDF goes through the same reader: its pages stay on this Mac
    in Local mode, and the extract is never empty."""
    from server.extract import pdf

    set_mode(mode)
    monkeypatch.setattr(pdf, "_render_pages", lambda content: ["page 1", "page 2"])
    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", lambda images: "")
    out = pdf.extract_pdf(b"%PDF-1.7", "")
    assert out.strip()
    if mode == "local":
        assert cloud_reader == [] and "haiku-text" not in out
    else:
        assert "haiku" in cloud_reader and "haiku-text" in out
