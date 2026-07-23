"""Type-aware attachment conversion.

Replaces the single ``MarkItDown().convert()`` call in server/attachments.py
with per-format handling:
  - MarkItDown stays the default for docx/pptx/html/csv/json/xml/code/epub/etc.
  - PDFs gain an OCR fallback for scans (server/extract/pdf.py).
  - Large spreadsheets become a schema + sample (server/extract/sheet.py).
  - Images get OCR'd text for text-only models (server/extract/image.py).
Every converted document also gets a heading outline (server/extract/outline.py)
so the chat layer can inject large files by outline + section instead of a blunt
character truncation.
"""

import logging
import os
import tempfile

from server.extract.image import ocr_image_bytes  # re-exported
from server.extract.outline import build_outline, get_section  # re-exported

log = logging.getLogger("whisper-studio")

__all__ = ["convert_document", "build_outline", "get_section", "ocr_image_bytes"]


def convert_document(content: bytes, ext: str, filename: str) -> str:
    """Blocking: convert an uploaded document to Markdown text.

    Runs MarkItDown first (its output is reused by the PDF/spreadsheet
    specializers), then dispatches by extension. Callers on the event loop must
    wrap this in ``asyncio.to_thread``.
    """
    md_text = ""
    tmp_path = None
    try:
        from markitdown import MarkItDown

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        md_text = MarkItDown().convert(tmp_path).text_content or ""
    except Exception as e:
        # MarkItDown often raises (or returns nothing) on scanned PDFs and odd
        # files. PDFs still get an OCR pass below; others surface the error.
        log.warning("markitdown failed for %s: %s", filename, e)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError as unlink_err:
                log.debug("attachment temp cleanup failed for %s: %s", tmp_path, unlink_err)

    if ext == ".pdf":
        from server.extract.pdf import extract_pdf

        return extract_pdf(content, md_text)
    if ext in (".xlsx", ".xls"):
        from server.extract.sheet import extract_xlsx

        return extract_xlsx(content, md_text)
    if ext == ".csv":
        from server.extract.sheet import extract_csv

        return extract_csv(content, md_text)
    return md_text or "[No content extracted]"
