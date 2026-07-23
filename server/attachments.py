import asyncio
import base64
import io
import json
import logging
import time
import uuid

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

from server.extract import build_outline, convert_document, ocr_image_bytes
from server.extract.media import extract_media, is_media_ext

log = logging.getLogger("whisper-studio")

router = APIRouter(tags=["attachments"])

# In-memory storage with TTL. 3 hours so a message's files survive long
# enough to be re-attached on a later regenerate / edit-resend within the
# same running session.
attachments: dict[str, dict] = {}
ATTACHMENT_TTL = 3 * 3600
MAX_FILE_SIZE = 50 * 1024 * 1024
# Audio/video are transcribed to text (only the transcript is kept), so they
# get a larger cap than documents whose bytes we hold in memory.
MEDIA_MAX_FILE_SIZE = 200 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Legacy binary Office formats (.doc / .ppt) are deliberately absent: MarkItDown
# cannot convert them, so they used to produce a silently empty "[No content]"
# document. Left out here they fall to the binary-reject path and surface a
# clear "unsupported file type" error instead. Their modern OOXML replacements
# (.docx / .pptx) stay because MarkItDown does handle those.
MARKITDOWN_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".csv",
    ".html",
    ".epub",
    ".txt",
    ".md",
    ".json",
    ".xml",
    ".zip",
}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".css", ".yaml", ".yml"}
# Extensions whose extracted text is Markdown/prose with genuine "#" headings,
# so a heading outline is meaningful. Everything else — code (where a leading
# "#" is a comment), plain text (.txt and the UTF-8 fallback), structured data
# (.json/.xml/.zip), and spreadsheet dumps (.csv/.xlsx/.xls) — is skipped so a
# language comment or literal never masquerades as a heading in the outline.
OUTLINE_EXTENSIONS = {".md", ".pdf", ".docx", ".pptx", ".html", ".epub"}

# Image normalization for Claude/Bedrock. Claude internally downsamples images
# whose long edge exceeds ~1568px, so we do that ourselves ONCE at high quality
# rather than shipping an oversized image it re-compresses (which softens fine
# text and hurts OCR). Only ever shrinks, never enlarges.
IMAGE_MAX_DIM = 1568  # long-edge cap (Anthropic's recommended max)
IMAGE_JPEG_QUALITY = 90  # high quality so small text stays readable
IMAGE_REENCODE_BYTES = 3_500_000  # above this, re-encode even if dimensions are ok
IMAGE_HARD_BYTES = 4_500_000  # keep under Bedrock's ~5MB/image hard limit


def _prepare_image(content: bytes, media_type: str) -> tuple[bytes, str]:
    """Return (data, media_type) sized for Claude, downsizing only when needed.

    Small, simple images pass through untouched, so a PNG screenshot stays
    lossless for the crispest OCR. Larger ones get their long edge capped at
    IMAGE_MAX_DIM and re-encoded: PNG sources stay PNG when that fits under the
    hard byte cap, otherwise fall back to high-quality JPEG.
    """
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except Exception:
        return content, media_type  # not decodable here; ship raw (best effort)

    longest = max(img.size)
    oversized = longest > IMAGE_MAX_DIM
    heavy = len(content) > IMAGE_REENCODE_BYTES
    alpha_or_palette = img.mode in ("RGBA", "P", "LA")
    if not (oversized or heavy or alpha_or_palette):
        return content, media_type  # already small and simple: keep as-is

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if oversized:
        scale = IMAGE_MAX_DIM / longest
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )

    def _encode(fmt: str, **kw) -> bytes:
        b = io.BytesIO()
        img.save(b, format=fmt, **kw)
        return b.getvalue()

    if media_type == "image/png":
        data, out_media = _encode("PNG", optimize=True), "image/png"
        if len(data) > IMAGE_HARD_BYTES:  # lossless too big: switch to JPEG
            data, out_media = _encode("JPEG", quality=IMAGE_JPEG_QUALITY), "image/jpeg"
    else:
        data, out_media = _encode("JPEG", quality=IMAGE_JPEG_QUALITY), "image/jpeg"

    q = IMAGE_JPEG_QUALITY
    while out_media == "image/jpeg" and len(data) > IMAGE_HARD_BYTES and q > 55:
        q -= 10
        data = _encode("JPEG", quality=q)

    # If we only touched it for heaviness (not a resize) and the re-encode came
    # out no smaller, keep the original bytes.
    if not oversized and not alpha_or_palette and len(data) >= len(content):
        return content, media_type
    return data, out_media


def _make_document_record(filename: str, text: str, outline: bool = True) -> dict:
    """Build an in-memory document record, computing a heading outline so the
    chat layer can inject large files by outline + section rather than a blunt
    character truncation.

    ``outline`` gates outline building: it must be False for code and plaintext
    documents, where a leading ``#`` is a language comment or literal rather
    than a Markdown heading and would otherwise yield a garbage outline."""
    outline_md, sections = build_outline(text) if outline else ("", [])
    return {
        "kind": "document",
        "filename": filename,
        "text": text,
        "outline": outline_md,
        "sections": sections,
        "created": time.time(),
    }


async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [k for k, v in attachments.items() if now - v["created"] > ATTACHMENT_TTL]
        for k in expired:
            del attachments[k]


@router.post("/api/upload")
async def upload_endpoint(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        content = await f.read()
        filename = f.filename or "unknown"
        ctype = f.content_type or ""
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        cap = MEDIA_MAX_FILE_SIZE if is_media_ext(ext) else MAX_FILE_SIZE
        if len(content) > cap:
            return Response(
                content=json.dumps(
                    {"error": f"File {filename} exceeds {cap // (1024 * 1024)}MB limit"}
                ),
                status_code=400,
                media_type="application/json",
            )

        aid = str(uuid.uuid4())

        if ctype in ALLOWED_IMAGE_TYPES:
            # Cap the long edge at ~1568px and re-encode at high quality only
            # when needed (see _prepare_image) so text stays OCR-able.
            img_data, media_type = await asyncio.to_thread(_prepare_image, content, ctype)
            # OCR the image so text-only chat models (the local Gemma build)
            # get readable text; vision models still receive the image block.
            ocr_text = await asyncio.to_thread(ocr_image_bytes, img_data)
            attachments[aid] = {
                "kind": "image",
                "filename": filename,
                "media_type": media_type,
                "data": base64.b64encode(img_data).decode(),
                "ocr_text": ocr_text,
                "created": time.time(),
            }
            results.append({"id": aid, "filename": filename, "type": "image"})
        elif is_media_ext(ext):
            # Audio/video: transcribe locally (and OCR sampled video frames).
            # Stored as a document so the transcript flows through the outline
            # and section-fetch path like any other large text attachment.
            # Video additionally retains a bounded set of downscaled keyframes
            # (hybrid understanding): the transcript + OCR text serve text-only
            # models, while vision models also get the frames as image blocks.
            text, frames = await asyncio.to_thread(extract_media, content, ext, filename)
            rec = _make_document_record(filename, text)
            if frames:
                rec["frames"] = frames
            attachments[aid] = rec
            results.append(
                {
                    "id": aid,
                    "filename": filename,
                    "type": ext.lstrip("."),
                    "frames": len(frames),
                }
            )
        elif ext in MARKITDOWN_EXTENSIONS or ext in CODE_EXTENSIONS:
            text = await asyncio.to_thread(convert_document, content, ext, filename)
            # Only Markdown/prose formats get a heading outline; code and data
            # dumps (where "#" is a comment or literal) skip it.
            attachments[aid] = _make_document_record(
                filename, text, outline=ext in OUTLINE_EXTENSIONS
            )
            results.append({"id": aid, "filename": filename, "type": ext.lstrip(".")})
        else:
            # Text fallback: any file whose bytes decode as UTF-8 is treated as
            # a plaintext document. This covers the long tail of text formats
            # (.log, .sql, .sh, .tsx, .jsx, .toml, .ini, .env, .conf, …) that
            # aren't in the markitdown/code extension lists. Genuinely binary
            # files (decode fails) are still rejected.
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                return Response(
                    content=json.dumps({"error": f"Unsupported file type: {filename}"}),
                    status_code=400,
                    media_type="application/json",
                )
            # Plaintext fallback: never outline (a leading "#" here is not a
            # Markdown heading).
            attachments[aid] = _make_document_record(
                filename, text or "[Empty file]", outline=False
            )
            results.append({"id": aid, "filename": filename, "type": ext.lstrip(".") or "text"})

    return {"attachments": results}
