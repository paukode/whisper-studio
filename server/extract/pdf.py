"""PDF extraction: MarkItDown's text layer first, OCR fallback for scans.

MarkItDown (pdfplumber to pdfminer.six) only reads an embedded text layer, so a
scanned/image-only PDF comes back empty. When that happens we rasterize the
pages with pypdfium2 (BSD/Apache, license-clean) and OCR them.
"""

import logging

log = logging.getLogger("whisper-studio")

# A born-digital PDF yields plenty of text; a scanned one yields ~nothing.
_SCANNED_TEXT_THRESHOLD = 100  # non-whitespace chars
_MAX_OCR_PAGES = 30  # bound OCR work so uploads stay responsive
_RENDER_SCALE = 2.0  # ~144 DPI: good OCR accuracy vs speed


def extract_pdf(content: bytes, markitdown_text: str) -> str:
    """Return ``markitdown_text`` when it's substantive, otherwise OCR the pages."""
    stripped = "".join((markitdown_text or "").split())
    if len(stripped) >= _SCANNED_TEXT_THRESHOLD:
        return markitdown_text

    images = _render_pages(content)
    if not images:
        return markitdown_text or "[No content extracted]"

    from server.extract.ocr import ocr_images

    text = ocr_images(images)
    note = ""
    if len(images) >= _MAX_OCR_PAGES:
        note = f"\n\n[OCR limited to the first {_MAX_OCR_PAGES} pages.]"
    return (text or "[No text recognized]") + note


def _render_pages(content: bytes):
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        log.warning("pypdfium2 unavailable: %s", e)
        return []

    images = []
    pdf = None
    try:
        pdf = pdfium.PdfDocument(content)
        for i in range(min(len(pdf), _MAX_OCR_PAGES)):
            bitmap = pdf[i].render(scale=_RENDER_SCALE)
            images.append(bitmap.to_pil())
    except Exception as e:
        log.warning("PDF rasterize failed: %s", e)
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass
    return images
