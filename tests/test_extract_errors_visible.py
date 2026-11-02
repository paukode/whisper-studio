"""Document extraction reports a failure as an error marker instead of
returning "no content", and the index never stores that marker as text."""

from __future__ import annotations

import pytest

import server.extract as extract
from server.extract import sheet


class _BrokenMarkItDown:
    def convert(self, path):
        raise RuntimeError("bad docx")


def test_a_failed_document_returns_an_error_marker(monkeypatch):
    import markitdown

    monkeypatch.setattr(markitdown, "MarkItDown", _BrokenMarkItDown)
    out = extract.convert_document(b"x", ".docx", "report.docx")
    assert out == "[Error extracting report.docx: bad docx]"


def test_a_corrupt_scanned_pdf_returns_an_error_marker(monkeypatch):
    import markitdown

    monkeypatch.setattr(markitdown, "MarkItDown", _BrokenMarkItDown)
    out = extract.convert_document(b"not a pdf", ".pdf", "scan.pdf")
    assert out.startswith("[Error extracting scan.pdf: ")


def test_xls_keeps_the_markitdown_table(monkeypatch):
    """openpyxl cannot read OLE2 .xls, so a .xls never goes to extract_xlsx."""
    import markitdown

    class _Md:
        def convert(self, path):
            class R:
                text_content = "| a |\n| --- |\n| 1 |"

            return R()

    monkeypatch.setattr(markitdown, "MarkItDown", _Md)

    def boom(*a, **k):
        raise AssertionError("extract_xlsx must not run for .xls")

    monkeypatch.setattr(sheet, "extract_xlsx", boom)
    assert extract.convert_document(b"\xd0\xcf\x11\xe0", ".xls", "old.xls").startswith("| a |")


def test_index_never_stores_an_error_marker():
    from server.index import pipeline

    with pytest.raises(RuntimeError, match="Error extracting"):
        pipeline._finish("[Error extracting a.docx: bad]")
    assert pipeline._finish("[No content extracted]") is None
