"""Type-aware attachment extraction: OCR fallback, spreadsheet schema+sample,
and the heading-outline / section-fetch path.

OCR tests force the offline Apple Vision path (no Bedrock call) so they're
deterministic. The app is macOS-only; if ``ocrmac`` is somehow unavailable the
OCR tests skip rather than fail.
"""

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

import server.attachments as A
import server.extract.ocr as ocrmod
from server.extract import build_outline, convert_document, get_section
from server.extract.sheet import extract_csv, extract_xlsx

app = FastAPI()
app.include_router(A.router)
client = TestClient(app)

try:
    import ocrmac  # noqa: F401

    _OCR_OK = True
except Exception:
    _OCR_OK = False

needs_vision = pytest.mark.skipif(not _OCR_OK, reason="ocrmac (Apple Vision) unavailable")


@pytest.fixture(autouse=True)
def force_local_ocr(monkeypatch):
    # Never hit Bedrock in tests: always resolve OCR to Apple Vision.
    monkeypatch.setattr(ocrmod, "_aws_available", lambda: False)


def _text_image(lines, width=820):
    img = Image.new("RGB", (width, 60 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    y = 25
    for ln in lines:
        d.text((25, y), ln, fill="black", font=font)
        y += 60
    return img


# --- OCR: scanned (image-only) PDF -----------------------------------------


@needs_vision
def test_scanned_pdf_falls_back_to_ocr():
    # A PIL image saved as PDF has no text layer, so MarkItDown returns nothing
    # and the extractor must OCR the rasterized page.
    buf = io.BytesIO()
    _text_image(["INVOICE 84412", "Total 1500 USD"]).save(buf, format="PDF")
    text = convert_document(buf.getvalue(), ".pdf", "scan.pdf")
    assert "84412" in text
    assert "INVOICE" in text.upper()


@needs_vision
def test_image_upload_stores_ocr_text():
    buf = io.BytesIO()
    _text_image(["HEADING 9921"]).save(buf, format="PNG")
    r = client.post("/api/upload", files={"files": ("shot.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    att = A.attachments[r.json()["attachments"][0]["id"]]
    assert att["kind"] == "image"
    assert "9921" in att.get("ocr_text", "")


# --- OCR engine ordering: Apple Vision first, Haiku only as fallback -------


def test_apple_vision_is_primary_haiku_not_called(monkeypatch):
    import server.extract.ocr as ocr

    monkeypatch.setattr(ocr, "_aws_available", lambda: True)  # creds present
    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", lambda imgs: "vision-text")
    called = {"haiku": False}

    def _haiku(imgs):
        called["haiku"] = True
        return "haiku-text"

    monkeypatch.setattr(ocr, "_ocr_with_haiku", _haiku)
    assert ocr.ocr_images(["img"]) == "vision-text"
    assert called["haiku"] is False  # Apple Vision is the default, even online


def test_haiku_fallback_when_apple_vision_empty(monkeypatch):
    import server.extract.ocr as ocr

    monkeypatch.setattr(ocr, "_aws_available", lambda: True)
    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", lambda imgs: "")
    monkeypatch.setattr(ocr, "_ocr_with_haiku", lambda imgs: "haiku-text")
    assert ocr.ocr_images(["img"]) == "haiku-text"


def test_no_haiku_without_creds(monkeypatch):
    import server.extract.ocr as ocr

    monkeypatch.setattr(ocr, "_aws_available", lambda: False)
    monkeypatch.setattr(ocr, "_ocr_with_apple_vision", lambda imgs: "")
    called = {"haiku": False}

    def _haiku(imgs):
        called["haiku"] = True
        return "x"

    monkeypatch.setattr(ocr, "_ocr_with_haiku", _haiku)
    assert ocr.ocr_images(["img"]) == ""
    assert called["haiku"] is False  # never call Bedrock without creds


# --- spreadsheets: schema + sample for large sheets ------------------------


def test_large_csv_becomes_schema_sample():
    rows = ["a,b,c"] + [f"{i},x{i},{i * 2}" for i in range(5000)]
    csv_bytes = "\n".join(rows).encode()
    huge_md = "z" * 70_000  # pretend markitdown produced a giant table
    out = extract_csv(csv_bytes, huge_md)
    assert out.startswith("[Large CSV: 5000 data rows x 3 cols")
    assert len(out) < 2000  # collapsed, not the 70k dump


def test_small_csv_keeps_markitdown_output():
    out = extract_csv(b"a,b\n1,2\n", "| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert out == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_large_xlsx_becomes_schema_sample():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["id", "name", "amount"])
    for i in range(1000):
        ws.append([i, f"row{i}", i * 10])
    buf = io.BytesIO()
    wb.save(buf)
    out = extract_xlsx(buf.getvalue(), "y" * 70_000)
    assert "Sheet: Data" in out
    assert "x 3 cols" in out  # 1001 rows (header + 1000) reported by openpyxl
    assert "| id | name | amount |" in out
    assert "| 0 | row0 | 0 |" in out  # first sampled data row
    assert "row999" not in out  # only the first sample window, not the tail
    assert len(out) < 3000


# --- outline + section slicing ---------------------------------------------


def test_build_outline_and_get_section():
    doc = "# Intro\nhello\n## Details\nbody\n## Summary\nend"
    outline, sections = build_outline(doc)
    assert "1. Intro" in outline
    assert "2. Details" in outline
    assert len(sections) == 3
    assert get_section(doc, sections, 2) == "## Details\nbody\n"
    assert get_section(doc, sections, 9) is None


def test_outline_empty_for_headingless_text():
    outline, sections = build_outline("just some plain text with no headings")
    assert outline == ""
    assert sections == []


# --- analyze_document section fetch ----------------------------------------


def test_analyze_document_fetches_section():
    from server.executors.content import exec_analyze_document

    doc = "# A\naaa\n## B\nbbb\n## C\nccc"
    outline, sections = build_outline(doc)
    store = {
        "x": {
            "kind": "document",
            "filename": "f.md",
            "text": doc,
            "outline": outline,
            "sections": sections,
        }
    }
    out = exec_analyze_document(
        {"filename": "f.md", "question": "what?", "section": "2"},
        "",
        store,
    )
    assert "## B\nbbb" in out
    # Without a section it returns the whole doc.
    full = exec_analyze_document({"filename": "f.md", "question": "what?"}, "", store)
    assert "aaa" in full and "ccc" in full
