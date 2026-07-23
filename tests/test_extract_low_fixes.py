"""Regression tests for two LOW-severity attachment-extraction fixes.

(1) Heading outlines must only be built for Markdown/prose documents. Code and
    plaintext attachments (where a leading ``#`` is a language comment or
    literal, not a Markdown heading) must NOT produce a spurious outline, while
    genuine Markdown still outlines correctly.

(2) Legacy binary Office formats (.doc / .ppt) that MarkItDown cannot convert
    must hit the binary-reject path with a clear "unsupported file type" error
    rather than silently becoming an empty document. Their modern OOXML
    replacements (.docx / .pptx) must still route to MarkItDown.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.attachments as A


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(A.router)
    return TestClient(app)


# --- fix 1: outline only for Markdown/prose --------------------------------


def test_code_file_produces_no_spurious_outline(monkeypatch):
    # A Python file whose comments start with "#". These are language comments,
    # NOT Markdown headings, so they must not seed a garbage outline.
    code = "# not a heading\nimport os\n\n# another comment\ndef f():\n    return 1\n"
    # Isolate the outline-gating logic from MarkItDown's code handling: the
    # converter just echoes the source text back.
    monkeypatch.setattr(A, "convert_document", lambda content, ext, filename: code)

    client = _client()
    r = client.post("/api/upload", files={"files": ("script.py", code.encode(), "text/x-python")})
    assert r.status_code == 200, r.text
    stored = A.attachments[r.json()["attachments"][0]["id"]]
    assert stored["kind"] == "document"
    assert code in stored["text"]  # content preserved
    assert stored["outline"] == ""  # no headings invented from "# comment" lines
    assert stored["sections"] == []


def test_markdown_file_still_outlines(monkeypatch):
    md = "# Intro\nhello\n## Details\nbody\n## Summary\nend"
    monkeypatch.setattr(A, "convert_document", lambda content, ext, filename: md)

    client = _client()
    r = client.post("/api/upload", files={"files": ("doc.md", md.encode(), "text/markdown")})
    assert r.status_code == 200, r.text
    stored = A.attachments[r.json()["attachments"][0]["id"]]
    assert stored["kind"] == "document"
    assert "1. Intro" in stored["outline"]
    assert "2. Details" in stored["outline"]
    assert len(stored["sections"]) == 3


def test_plaintext_fallback_never_outlines():
    # A .log file with "#"-prefixed lines takes the UTF-8 text fallback (not in
    # the markitdown/code lists). Those "#" lines are not Markdown headings.
    body = b"# config dump\nkey = value\n# section marker\nother = 1\n"
    client = _client()
    r = client.post("/api/upload", files={"files": ("app.log", body, "text/plain")})
    assert r.status_code == 200, r.text
    stored = A.attachments[r.json()["attachments"][0]["id"]]
    assert stored["kind"] == "document"
    assert stored["outline"] == ""
    assert stored["sections"] == []


def test_make_document_record_outline_flag():
    doc = "# heading-like\nline\n## sub\nmore"
    with_outline = A._make_document_record("f.md", doc, outline=True)
    assert with_outline["outline"] != ""
    assert with_outline["sections"]

    without_outline = A._make_document_record("f.py", doc, outline=False)
    assert without_outline["outline"] == ""
    assert without_outline["sections"] == []
    assert without_outline["text"] == doc  # text is untouched regardless


# --- fix 2: legacy .doc / .ppt rejected, .docx / .pptx still handled --------


def test_legacy_doc_rejected_not_silently_empty():
    assert ".doc" not in A.MARKITDOWN_EXTENSIONS
    # OLE compound-file magic + a lone 0xff byte guarantees non-UTF-8 (binary).
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 32
    before = len(A.attachments)

    client = _client()
    r = client.post(
        "/api/upload",
        files={"files": ("legacy.doc", payload, "application/msword")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["error"]
    # Nothing stored: it is rejected outright, not kept as an empty document.
    assert len(A.attachments) == before


def test_legacy_ppt_rejected_not_silently_empty():
    assert ".ppt" not in A.MARKITDOWN_EXTENSIONS
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 32
    client = _client()
    r = client.post(
        "/api/upload",
        files={"files": ("deck.ppt", payload, "application/vnd.ms-powerpoint")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["error"]


def test_docx_still_routes_to_markitdown(monkeypatch):
    assert ".docx" in A.MARKITDOWN_EXTENSIONS
    marker = "MARKITDOWN CONVERTED CONTENT"
    seen = {}

    def _fake_convert(content, ext, filename):
        seen["ext"] = ext
        return marker

    monkeypatch.setattr(A, "convert_document", _fake_convert)

    client = _client()
    r = client.post(
        "/api/upload",
        files={"files": ("report.docx", b"PK\x03\x04 fake docx bytes", "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert seen["ext"] == ".docx"  # routed through the MarkItDown converter
    stored = A.attachments[r.json()["attachments"][0]["id"]]
    assert stored["text"] == marker


def test_pptx_still_routes_to_markitdown(monkeypatch):
    assert ".pptx" in A.MARKITDOWN_EXTENSIONS
    marker = "PPTX CONVERTED CONTENT"
    monkeypatch.setattr(A, "convert_document", lambda content, ext, filename: marker)

    client = _client()
    r = client.post(
        "/api/upload",
        files={"files": ("slides.pptx", b"PK\x03\x04 fake pptx bytes", "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    stored = A.attachments[r.json()["attachments"][0]["id"]]
    assert stored["text"] == marker
