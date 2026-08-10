"""create_docx / create_pptx / create_xlsx / create_pdf.

These exercise the REAL converters (macOS textutil, python-pptx, openpyxl,
reportlab) rather than mocking them — the whole point of these tools is that
they produce genuine, round-trippable files, so a mocked subprocess/library
call would not catch a broken invocation.

The no-workspace case mirrors ws_create_file exactly: the same
[WS_WORKSPACE_PROMPT] sentinel, and — the thing that would silently break if
someone reordered a check — no subprocess/library call happens before that
sentinel is returned.

A successful build does NOT write to disk directly: it returns a
[WS_APPROVAL] pause (action=create_document, content_b64=<bytes>), the same
human-in-the-loop gate ws_create_file goes through. The actual write is
exercised separately against server.approval.bootstrap._do_create_document.
"""

import asyncio
import base64
import io
import json
import zipfile
from unittest.mock import patch

import pytest

from server.approval.bootstrap import _do_create_document
from server.documents.executors import (
    _exec_create_docx,
    _exec_create_pdf,
    _exec_create_pptx,
    _exec_create_xlsx,
)


def _run(coro):
    return asyncio.run(coro)


def _parse_prompt(output: str) -> dict:
    assert output.startswith("[WS_WORKSPACE_PROMPT]"), output
    return json.loads(output[len("[WS_WORKSPACE_PROMPT]") :])


def _parse_approval(output: str) -> dict:
    assert output.startswith("[WS_APPROVAL]"), output
    payload = json.loads(output[len("[WS_APPROVAL]") :])
    assert payload["action"] == "create_document"
    return payload


def _decoded_bytes(payload: dict) -> bytes:
    return base64.b64decode(payload["content_b64"])


# ── No workspace connected: sentinel, no shelling out / no library calls ──


def test_create_docx_no_workspace_emits_sentinel_and_never_shells_out(monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: None)
    with patch("subprocess.run") as mock_run:
        out = _exec_create_docx({"path": "report.docx", "html_content": "<p>hi</p>"}, "", {})
    mock_run.assert_not_called()
    payload = _parse_prompt(out)
    assert payload["reason"] == "no_workspace"
    assert payload["tool_name"] == "create_docx"
    assert payload["tool_input"]["path"] == "report.docx"


def test_create_pptx_no_workspace_emits_sentinel(monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: None)
    out = _exec_create_pptx(
        {"path": "deck.pptx", "slides": [{"title": "T", "bullets": ["a"]}]}, "", {}
    )
    payload = _parse_prompt(out)
    assert payload["reason"] == "no_workspace"
    assert payload["tool_name"] == "create_pptx"


def test_create_xlsx_no_workspace_emits_sentinel(monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: None)
    out = _exec_create_xlsx(
        {"path": "data.xlsx", "sheets": [{"name": "Sheet1", "rows": [[1, 2]]}]}, "", {}
    )
    payload = _parse_prompt(out)
    assert payload["reason"] == "no_workspace"
    assert payload["tool_name"] == "create_xlsx"


def test_create_pdf_no_workspace_emits_sentinel(monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: None)
    out = _exec_create_pdf({"path": "summary.pdf", "title": "T", "paragraphs": ["hello"]}, "", {})
    payload = _parse_prompt(out)
    assert payload["reason"] == "no_workspace"
    assert payload["tool_name"] == "create_pdf"


def test_create_docx_path_outside_workspace_emits_sentinel(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(ws))
    with patch("subprocess.run") as mock_run:
        out = _exec_create_docx({"path": "../outside.docx", "html_content": "<p>hi</p>"}, "", {})
    mock_run.assert_not_called()
    payload = _parse_prompt(out)
    assert payload["reason"] == "outside_workspace"


# ── Real conversions: build succeeds, pauses for approval, never touches disk ──


def test_create_docx_produces_real_ooxml_and_pauses_for_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    out = _exec_create_docx(
        {
            "path": "report.docx",
            "html_content": "<h1>Title</h1><p>Body text with <b>bold</b>.</p>",
        },
        "",
        {},
    )
    payload = _parse_approval(out)
    assert payload["path"] == "report.docx"
    assert payload["format"] == "docx"
    assert not (tmp_path / "report.docx").exists(), "must not write before approval"

    data = _decoded_bytes(payload)
    assert payload["size"] == len(data) > 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        assert "word/document.xml" in names
        assert "[Content_Types].xml" in names
        doc_xml = z.read("word/document.xml").decode("utf-8")
        assert "Body text" in doc_xml


def test_create_docx_rejects_empty_html(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    out = _exec_create_docx({"path": "empty.docx", "html_content": "   "}, "", {})
    assert out.startswith("Error"), out
    assert not (tmp_path / "empty.docx").exists()


def test_create_pptx_round_trips_with_python_pptx(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    slides = [
        {"title": "Intro", "bullets": ["First point", "Second point"]},
        {"title": "Conclusion", "bullets": ["Wrap up"]},
    ]
    out = _exec_create_pptx({"path": "deck.pptx", "slides": slides}, "", {})
    payload = _parse_approval(out)
    assert payload["format"] == "pptx"
    assert not (tmp_path / "deck.pptx").exists(), "must not write before approval"
    data = _decoded_bytes(payload)
    assert len(data) > 0

    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 2
    titles = [s.shapes.title.text for s in prs.slides]
    assert titles == ["Intro", "Conclusion"]
    first_slide_bullets = [p.text for p in prs.slides[0].placeholders[1].text_frame.paragraphs]
    assert first_slide_bullets == ["First point", "Second point"]


def test_create_xlsx_round_trips_with_openpyxl(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    sheets = [
        {"name": "Sales", "rows": [["Item", "Qty"], ["Widget", 3]]},
        {"name": "Notes", "rows": [["free text"]]},
    ]
    out = _exec_create_xlsx({"path": "data.xlsx", "sheets": sheets}, "", {})
    payload = _parse_approval(out)
    assert payload["format"] == "xlsx"
    assert not (tmp_path / "data.xlsx").exists(), "must not write before approval"
    data = _decoded_bytes(payload)
    assert len(data) > 0

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Sales", "Notes"]
    ws = wb["Sales"]
    assert ws["A1"].value == "Item"
    assert ws["B1"].value == "Qty"
    assert ws["A2"].value == "Widget"
    assert ws["B2"].value == 3
    assert wb["Notes"]["A1"].value == "free text"


def test_create_pdf_produces_valid_pdf_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    out = _exec_create_pdf(
        {
            "path": "summary.pdf",
            "title": "Quarterly Summary",
            "paragraphs": ["Revenue is up.", "Costs < projections & on track."],
        },
        "",
        {},
    )
    payload = _parse_approval(out)
    assert payload["format"] == "pdf"
    assert not (tmp_path / "summary.pdf").exists(), "must not write before approval"
    data = _decoded_bytes(payload)
    assert len(data) > 0
    assert data[:5] == b"%PDF-"


def test_create_pdf_escapes_special_characters_without_dropping_content(tmp_path, monkeypatch):
    """A plain paragraph containing '<', '>', '&' must survive verbatim —
    reportlab's Paragraph treats unescaped input as its own mini-XML markup
    and will silently swallow or mangle text that looks like a tag."""
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    tricky = "<unknown>tag</unknown> AT&T reported 5 < 10 items"
    out = _exec_create_pdf({"path": "tricky.pdf", "title": "", "paragraphs": [tricky]}, "", {})
    payload = _parse_approval(out)
    data = _decoded_bytes(payload)

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    text = pdf[0].get_textpage().get_text_range()
    assert "<unknown>tag</unknown>" in text
    assert "AT&T reported 5 < 10 items" in text


def test_create_pdf_requires_title_or_paragraphs(tmp_path, monkeypatch):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    out = _exec_create_pdf({"path": "empty.pdf", "title": "", "paragraphs": []}, "", {})
    assert out.startswith("Error"), out
    assert not (tmp_path / "empty.pdf").exists()


@pytest.mark.parametrize(
    "executor,tool_input",
    [
        (_exec_create_pptx, {"path": "bad.pptx", "slides": []}),
        (_exec_create_xlsx, {"path": "bad.xlsx", "sheets": []}),
    ],
)
def test_empty_collection_inputs_are_rejected(tmp_path, monkeypatch, executor, tool_input):
    monkeypatch.setattr("server.documents.executors.get_workspace_path", lambda: str(tmp_path))
    out = executor(tool_input, "", {})
    assert out.startswith("Error"), out


# ── Approval click actually performs the write ─────────────────────────────


def test_do_create_document_writes_file_on_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    data = b"fake docx bytes for approval-write test"
    payload = {
        "path": "approved.docx",
        "content_b64": base64.b64encode(data).decode("ascii"),
        "format": "docx",
        "size": len(data),
    }
    outcome = _run(_do_create_document(payload))
    assert outcome.ok, outcome.error
    full = tmp_path / "approved.docx"
    assert full.is_file()
    assert full.read_bytes() == data
    assert "Created approved.docx" in (outcome.output or "")


def test_do_create_document_no_workspace_fails(monkeypatch):
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    outcome = _run(_do_create_document({"path": "x.docx", "content_b64": ""}))
    assert not outcome.ok
    assert "No workspace connected" in (outcome.error or "")


def test_do_create_document_rejects_path_outside_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(ws))
    outcome = _run(
        _do_create_document(
            {"path": "../outside.docx", "content_b64": base64.b64encode(b"x").decode("ascii")}
        )
    )
    assert not outcome.ok
    assert not (tmp_path / "outside.docx").exists()
