"""@register_executor handlers for the document-creation tools.

Two destination paths, mirroring save_file (server/workspace/executors.py):

- A workspace IS connected: `path` is workspace-relative, same no-workspace-
  escape contract as ws_create_file. The write goes through the
  "create_document" approval action (server/approval/bootstrap.py's
  _do_create_document), which re-resolves the workspace root at click time.
- No workspace connected: rather than forcing a full workspace connect for
  a single deliverable file, this pauses with the same lightweight
  "where should this go" prompt save_file uses (Documents/Downloads/a native
  save dialog), via the shared server.workspace.state.resolve_write_destination
  resolver (also used by ws_create_file, so every "make a new file" tool
  degrades identically with no workspace connected). Once the user picks a
  location, the model re-issues the same call with destination_path set,
  and the write goes through the existing "save_to_path" approval action
  (server/approval/bootstrap.py's _do_save_to_path) — no new approval
  action needed, it already handles an arbitrary absolute path, base64
  content, the sensitive-path denylist, and open-with/reveal registration.

Either way, the actual write only happens after a human clicks Yes on an
approval card — the executor never writes bytes directly. There is no diff
to preview (the model supplies HTML/slide-data/rows/paragraphs, not literal
file bytes), so the approval card shows a plain "Create <path> (<n> bytes)"
confirmation (preview="text") rather than a diff.
"""

import base64
import io
import json
import logging
import os
import subprocess
import tempfile
from xml.sax.saxutils import escape as _xml_escape

from server.executors import register_executor
from server.workspace.state import resolve_write_destination

log = logging.getLogger("whisper-studio")

_TEXTUTIL = "/usr/bin/textutil"
# Generous but bounded: this is HTML *source*, not the rendered document —
# a few MB of markup is already a very large document. Guards against
# shelling out to textutil with a runaway payload.
_MAX_HTML_CONTENT_BYTES = 5_000_000
_TEXTUTIL_TIMEOUT_SECONDS = 30


def _resolve_target(tool_name: str, tool_input: dict) -> tuple[str, str] | str:
    """Thin wrapper around the shared resolve_write_destination (server/
    workspace/state.py), adding the is-a-directory check specific to
    document tools — a docx/pptx/xlsx/pdf can never target an existing
    directory, unlike the generic resolver's callers."""
    resolved = resolve_write_destination(tool_name, tool_input)
    if isinstance(resolved, str):
        return resolved
    kind, target = resolved
    if kind == "workspace" and os.path.isdir(target):
        path = tool_input.get("path", "")
        return f"Error: {path} is an existing directory, not a file path."
    return resolved


def _approval_result(resolved: tuple[str, str], relative_path: str, data: bytes, fmt: str) -> str:
    """Package generated document bytes as a [WS_APPROVAL] pause instead of
    writing them straight to disk — same human-in-the-loop gate every other
    workspace write tool goes through.

    `resolved` is _resolve_target's success tuple. For ("workspace", ...),
    packages a create_document action keyed by the workspace-relative path
    (server/approval/bootstrap.py's _do_create_document re-resolves the
    workspace root itself). For ("absolute", ...), packages a save_to_path
    action — the same one server/workspace/executors.py's save_file uses —
    keyed by the absolute destination path."""
    kind, target = resolved
    if kind == "absolute":
        payload = json.dumps(
            {
                "action": "save_to_path",
                "path": target,
                "content": base64.b64encode(data).decode("ascii"),
                "content_encoding": "base64",
                "filename": os.path.basename(target),
            }
        )
    else:
        payload = json.dumps(
            {
                "action": "create_document",
                "path": relative_path,
                "content_b64": base64.b64encode(data).decode("ascii"),
                "format": fmt,
                "size": len(data),
            }
        )
    return f"[WS_APPROVAL]{payload}"


@register_executor("create_docx", read_only=False, concurrent_safe=False)
def _exec_create_docx(tool_input, transcript, current_attachments):
    resolved = _resolve_target("create_docx", tool_input)
    if isinstance(resolved, str):
        return resolved
    path = tool_input.get("path", "")

    html_content = tool_input.get("html_content", "")
    if not html_content or not html_content.strip():
        return "Error: html_content is required and must be non-empty."
    encoded_len = len(html_content.encode("utf-8", errors="ignore"))
    if encoded_len > _MAX_HTML_CONTENT_BYTES:
        return (
            f"Error: html_content is too large ({encoded_len} bytes) — keep it under "
            f"{_MAX_HTML_CONTENT_BYTES} bytes."
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".docx", prefix="whisper_docx_")
    os.close(fd)
    try:
        try:
            result = subprocess.run(
                [
                    _TEXTUTIL,
                    "-stdin",
                    "-format",
                    "html",
                    "-convert",
                    "docx",
                    "-output",
                    tmp_path,
                ],
                input=html_content,
                capture_output=True,
                text=True,
                timeout=_TEXTUTIL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"Error: textutil timed out after {_TEXTUTIL_TIMEOUT_SECONDS}s."
        except OSError as e:
            return f"Error: could not run textutil: {e}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return f"Error: textutil failed converting HTML to docx: {detail}"
        try:
            with open(tmp_path, "rb") as f:
                data = f.read()
        except OSError as e:
            return f"Error reading converted docx: {e}"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not data:
        return "Error: textutil produced an empty docx file."
    return _approval_result(resolved, path, data, "docx")


@register_executor("create_pptx", read_only=False, concurrent_safe=False)
def _exec_create_pptx(tool_input, transcript, current_attachments):
    resolved = _resolve_target("create_pptx", tool_input)
    if isinstance(resolved, str):
        return resolved
    path = tool_input.get("path", "")

    slides = tool_input.get("slides")
    if not isinstance(slides, list) or not slides:
        return "Error: slides must be a non-empty list of {title, bullets}."
    for i, slide_spec in enumerate(slides):
        if not isinstance(slide_spec, dict):
            return f"Error: slides[{i}] must be an object with 'title' and 'bullets'."
        if not isinstance(slide_spec.get("bullets") or [], list):
            return f"Error: slides[{i}].bullets must be a list of strings."

    from pptx import Presentation

    try:
        prs = Presentation()
        layout = prs.slide_layouts[1]  # "Title and Content"
        for slide_spec in slides:
            title = str(slide_spec.get("title", "") or "")
            bullets = slide_spec.get("bullets") or []
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = title
            text_frame = slide.placeholders[1].text_frame
            text_frame.clear()
            for j, bullet in enumerate(bullets):
                para = text_frame.paragraphs[0] if j == 0 else text_frame.add_paragraph()
                para.text = str(bullet)
        buf = io.BytesIO()
        prs.save(buf)
    except Exception as e:
        return f"Error building pptx: {e}"

    return _approval_result(resolved, path, buf.getvalue(), "pptx")


@register_executor("create_xlsx", read_only=False, concurrent_safe=False)
def _exec_create_xlsx(tool_input, transcript, current_attachments):
    resolved = _resolve_target("create_xlsx", tool_input)
    if isinstance(resolved, str):
        return resolved
    path = tool_input.get("path", "")

    sheets = tool_input.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        return "Error: sheets must be a non-empty list of {name, rows}."
    for i, sheet_spec in enumerate(sheets):
        if not isinstance(sheet_spec, dict):
            return f"Error: sheets[{i}] must be an object with 'name' and 'rows'."
        if not isinstance(sheet_spec.get("rows") or [], list):
            return f"Error: sheets[{i}].rows must be a list of rows."

    from openpyxl import Workbook

    try:
        wb = Workbook()
        for i, sheet_spec in enumerate(sheets):
            name = str(sheet_spec.get("name") or f"Sheet{i + 1}")
            rows = sheet_spec.get("rows") or []
            worksheet = wb.active if i == 0 else wb.create_sheet()
            worksheet.title = name
            for row in rows:
                if not isinstance(row, list):
                    return f"Error: sheets[{i}].rows must be a list of lists."
                worksheet.append(row)
        buf = io.BytesIO()
        wb.save(buf)
    except Exception as e:
        return f"Error building xlsx: {e}"

    return _approval_result(resolved, path, buf.getvalue(), "xlsx")


@register_executor("create_pdf", read_only=False, concurrent_safe=False)
def _exec_create_pdf(tool_input, transcript, current_attachments):
    resolved = _resolve_target("create_pdf", tool_input)
    if isinstance(resolved, str):
        return resolved
    path = tool_input.get("path", "")

    title = str(tool_input.get("title", "") or "")
    paragraphs = tool_input.get("paragraphs")
    if not isinstance(paragraphs, list):
        return "Error: paragraphs must be a list of strings."
    if not title and not paragraphs:
        return "Error: provide a title and/or at least one paragraph."

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    def _flowable_text(raw: str) -> str:
        # paragraphs/title are plain text, not reportlab's mini-XML markup —
        # escape so a stray "<", ">", or "&" can't be mistaken for a tag
        # (which silently drops or mangles the surrounding text) and turn a
        # literal newline into a real line break within the paragraph.
        return _xml_escape(raw).replace("\n", "<br/>")

    try:
        styles = getSampleStyleSheet()
        flowables = []
        if title:
            flowables.append(Paragraph(_flowable_text(title), styles["Title"]))
            flowables.append(Spacer(1, 12))
        for i, para in enumerate(paragraphs):
            flowables.append(Paragraph(_flowable_text(str(para)), styles["Normal"]))
            if i != len(paragraphs) - 1:
                flowables.append(Spacer(1, 12))

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        doc.build(flowables)
    except Exception as e:
        return f"Error building pdf: {e}"

    return _approval_result(resolved, path, buf.getvalue(), "pdf")
