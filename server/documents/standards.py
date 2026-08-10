"""The app's standard document layout, applied by every document tool.

One place defines what "a normal-looking document" means so create_docx /
create_pptx / create_xlsx / create_pdf all produce consistent, professional
output by default, instead of each underlying library's own quirky default
(textutil: US Letter + whatever system font the HTML resolved to; reportlab:
US Letter 10pt; python-pptx: the ancient 4:3 slide size).

The standard:
- docx: A4 page, 1-inch margins, Calibri 11pt body, 16/13/12pt headings.
- pptx: 16:9 widescreen (PowerPoint's own modern default), Calibri theme.
- xlsx: Calibri 11 (openpyxl default), bold header row, frozen top row,
  column widths fitted to content.
- pdf: A4 page, 1-inch margins, 11pt body with comfortable leading.
"""

import io
import re
import zipfile

# ── docx ──────────────────────────────────────────────────────────────

# A4 in twips (1/20pt): 210x297mm.
_A4_PGSZ = '<w:pgSz w:w="11906" w:h="16838"/>'
# 1-inch margins all around, standard 0.5-inch header/footer.
_A4_PGMAR = (
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
    'w:header="720" w:footer="720" w:gutter="0"/>'
)

_PGSZ_RE = re.compile(r"<w:pgSz[^/]*/>")
_PGMAR_RE = re.compile(r"<w:pgMar[^/]*/>")

# Fonts the Cocoa HTML importer falls back to when Calibri isn't installed
# (it's a Microsoft font, absent on Macs without Office). The run properties
# in the emitted OOXML get rewritten to Calibri so the document opens with
# the intended font wherever it ends up; Word substitutes sensibly on
# systems that lack it. Deliberately narrow — Courier/monospace runs from
# <code> blocks are left alone.
_FALLBACK_FONTS_RE = re.compile(
    r'(w:(?:ascii|hAnsi|cs|eastAsia)=")'
    r"(?:Helvetica Neue|Helvetica|Times New Roman|Times|\.AppleSystemUIFont|\.SF NS(?: Text)?)"
    r'(")'
)

# The Cocoa HTML importer converts CSS point sizes at 96 DPI, inflating them
# by 4/3 (11pt CSS arrives as w:sz 29 = 14.5pt in Word). Scaling every run's
# size back by 0.75 restores the sizes the skeleton CSS asked for: body 11pt
# (w:sz 22), h1 16pt, h2 13pt, h3 12pt. The Cocoa writer emits the
# non-standard <w:sz-cs> spelling alongside <w:sz>; both are matched.
_SZ_RE = re.compile(r'(<w:sz(?:-cs)?\s+w:val=")(\d+)(")')


def _rescale_sz(match: re.Match) -> str:
    return f"{match.group(1)}{round(int(match.group(2)) * 0.75)}{match.group(3)}"


# Body/heading sizes the HTML skeleton asks the converter for. The Cocoa
# importer honors font-size reliably (it becomes w:sz on each run) even when
# the font-family needs the post-conversion rewrite above.
DOCX_STANDARD_CSS = (
    "body { font-family: Calibri, 'Helvetica Neue', sans-serif; font-size: 11pt; } "
    "h1 { font-size: 16pt; } h2 { font-size: 13pt; } h3 { font-size: 12pt; } "
    "table { border-collapse: collapse; } td, th { border: 0.5pt solid #999999; padding: 3pt; }"
)


def docx_html_skeleton(body_html: str) -> str:
    """Wrap tool-supplied body HTML in the standard skeleton. Content that
    is already a full document (has its own <html>) is trusted as-is."""
    if "<html" in body_html.lower():
        return body_html
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{DOCX_STANDARD_CSS}</style></head><body>{body_html}</body></html>"
    )


def normalize_docx_standard(data: bytes) -> bytes:
    """Rewrite a textutil-produced docx to the standard layout: A4 page,
    1-inch margins, Calibri run fonts (see _FALLBACK_FONTS_RE). Everything
    else in the archive passes through untouched."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "word/document.xml":
                xml = payload.decode("utf-8")
                if _PGSZ_RE.search(xml):
                    xml = _PGSZ_RE.sub(_A4_PGSZ, xml)
                    xml = _PGMAR_RE.sub(_A4_PGMAR, xml)
                elif "</w:body>" in xml:
                    # No section properties at all — append a minimal one.
                    xml = xml.replace(
                        "</w:body>",
                        f"<w:sectPr>{_A4_PGSZ}{_A4_PGMAR}</w:sectPr></w:body>",
                    )
                xml = _FALLBACK_FONTS_RE.sub(r"\1Calibri\2", xml)
                xml = _SZ_RE.sub(_rescale_sz, xml)
                payload = xml.encode("utf-8")
            out.writestr(item, payload)
    return out_buf.getvalue()


# ── pptx ──────────────────────────────────────────────────────────────

# PowerPoint's own 16:9 dimensions in EMU (13.333 x 7.5 inches).
PPTX_SLIDE_WIDTH_EMU = 12192000
PPTX_SLIDE_HEIGHT_EMU = 6858000


def apply_pptx_standard(prs) -> None:
    """Switch a python-pptx Presentation to 16:9 — its bundled template
    still defaults to 4:3. The template's Office theme already uses
    Calibri, so fonts need no per-run overrides."""
    prs.slide_width = PPTX_SLIDE_WIDTH_EMU
    prs.slide_height = PPTX_SLIDE_HEIGHT_EMU


def fit_pptx_placeholders(prs, slide) -> None:
    """Stretch a freshly-added slide's placeholders across the 16:9 width.
    python-pptx copies placeholder geometry from the 4:3 layout, which
    would leave a third of every widescreen slide empty on the right."""
    for shape in slide.placeholders:
        shape.width = prs.slide_width - 2 * shape.left


# ── xlsx ──────────────────────────────────────────────────────────────

XLSX_MIN_COL_WIDTH = 10
XLSX_MAX_COL_WIDTH = 60


def apply_xlsx_standard(worksheet) -> None:
    """Standard worksheet polish: bold header row, frozen top row, and
    column widths fitted to content (bounded so one long cell can't blow a
    column out to absurd width). openpyxl's defaults already give Calibri
    11 for cell text."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if worksheet.max_row < 1 or worksheet.max_column < 1:
        return
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    worksheet.freeze_panes = "A2"
    for idx, column in enumerate(worksheet.iter_cols(), start=1):
        longest = max((len(str(c.value)) for c in column if c.value is not None), default=0)
        width = min(max(XLSX_MIN_COL_WIDTH, longest + 2), XLSX_MAX_COL_WIDTH)
        worksheet.column_dimensions[get_column_letter(idx)].width = width


# ── pdf ───────────────────────────────────────────────────────────────

PDF_BODY_FONT_SIZE = 11
PDF_BODY_LEADING = 15


def pdf_standard_styles():
    """reportlab stylesheet adjusted to the standard: 11pt body with
    comfortable leading (the sample sheet's Normal is a cramped 10/12).
    Callers pair this with an A4 SimpleDocTemplate."""
    from reportlab.lib.styles import getSampleStyleSheet

    styles = getSampleStyleSheet()
    styles["Normal"].fontSize = PDF_BODY_FONT_SIZE
    styles["Normal"].leading = PDF_BODY_LEADING
    return styles
