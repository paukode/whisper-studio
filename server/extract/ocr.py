"""OCR for scanned PDFs and text-heavy images.

Engine selection, in order:
  1. Apple Vision (native macOS OCR via the ``ocrmac`` package). The default,
     always tried first. Zero model download, ~0 RAM, fast on printed text,
     fully on-device and private. No Bedrock cost.
  2. Bedrock Claude Haiku vision, only as a fallback when Apple Vision fails or
     is unavailable AND AWS credentials resolve AND the app is not in Local
     mode.

We never call Bedrock without working credentials, never before Apple
Vision has had a chance, and never in Local mode (server/infrastructure/
cloud_guard.py): there an image Apple Vision cannot read stays unread, and
``local_mode_note`` gives the chat context a line saying so while the mode
lasts (server/chat/attachment_context.py renders it; it is never stored as the
image's text). This covers chat images, indexed images and scanned PDFs alike,
since all of them come through ``ocr_images``.
"""

import base64
import io
import json
import logging

log = logging.getLogger("whisper-studio")

# After a Bedrock auth/permission failure we stop trying Haiku for the rest of
# the process and go straight to Apple Vision, rather than hammering a denied
# endpoint on every upload.
_haiku_ocr_disabled = False

_OCR_PROMPT = (
    "Transcribe all text in these page images into clean GitHub-flavored "
    "Markdown. Preserve headings, lists, and tables. Output only the "
    "transcription, with no commentary or code fences."
)
# Claude accepts many images per request; stay well under the ceiling so a
# scanned PDF goes out in a single invoke.
_MAX_HAIKU_IMAGES = 20


def _aws_available() -> bool:
    """True if boto3 can resolve credentials WITHOUT a network call."""
    if _haiku_ocr_disabled:
        return False
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _pil_to_png_b64(img) -> str:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _ocr_with_haiku(images) -> str:
    from server.chat.infra import _get_bedrock_client, _get_chat_models

    model_id = _get_chat_models().get("haiku")
    if not model_id:
        raise RuntimeError("no haiku model configured")
    client = _get_bedrock_client()

    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _pil_to_png_b64(img)},
        }
        for img in images[:_MAX_HAIKU_IMAGES]
    ]
    content.append({"type": "text", "text": _OCR_PROMPT})

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": content}],
        }
    )
    from server.costs.calls import invoke_claude

    payload = invoke_claude(client, model_id=model_id, body=body, source="ocr")
    parts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _ocr_with_apple_vision(images) -> str:
    from ocrmac import ocrmac

    out = []
    for img in images:
        rgb = img if img.mode in ("RGB", "L") else img.convert("RGB")
        results = ocrmac.OCR(rgb, framework="vision").recognize()
        # Each result is (text, confidence, bbox). bbox is normalized with the
        # origin at the bottom-left (Vision convention), so a larger y is
        # higher up the page. Sort top-to-bottom, then left-to-right.
        lines = sorted(results, key=lambda r: (-r[2][1], r[2][0]))
        out.append("\n".join(text for text, _conf, _bbox in lines))
    return "\n\n".join(p for p in out if p).strip()


_CLOUD_READER = "The cloud OCR reader"


def local_mode_note() -> str:
    """For an image whose OCR came back empty: a bracketed line telling the
    reader that Local mode keeps the image off the cloud reader, else ``""``
    (outside Local mode an empty result means no reader found text). Read at
    context-build time, so it follows the current mode."""
    from server.infrastructure.cloud_guard import cloud_refusal

    refusal = cloud_refusal(_CLOUD_READER)
    return f"[No text was recognized on this Mac. {refusal}]" if refusal else ""


def ocr_images(images) -> str:
    """OCR a list of PIL images into Markdown text.

    Apple Vision (native, on-device) runs first and is the default. Haiku is
    only tried as a fallback when Apple Vision fails or is unavailable, only
    when AWS creds resolve, and never in Local mode. A successful-but-empty
    Apple Vision pass also falls through to Haiku deliberately: Apple Vision
    frequently returns nothing for images that do contain text, so Haiku is a
    second reader (see
    tests/test_attachment_extraction.py::test_haiku_fallback_when_apple_vision_empty).
    Returns an empty string if no path yields text.
    """
    global _haiku_ocr_disabled
    if not images:
        return ""
    # Apple Vision first: native macOS OCR, on-device, free, private.
    try:
        text = _ocr_with_apple_vision(images)
        if text:
            return text
    except Exception as e:
        log.warning("Apple Vision OCR failed (%s); trying Haiku fallback", e)
    # Local mode: the image never leaves this Mac, whatever Apple Vision found.
    # Checked before the credential lookup, so nothing about AWS is touched.
    from server.infrastructure.cloud_guard import cloud_allowed

    if not cloud_allowed():
        log.info("image OCR: Apple Vision found no text; Local mode skips the Haiku reader")
        return ""
    # Fallback: Bedrock Haiku, only when credentials are present.
    if _aws_available():
        try:
            return _ocr_with_haiku(images) or ""
        except Exception as e:
            log.warning("Haiku OCR fallback failed: %s", e)
            from botocore.exceptions import ClientError, NoCredentialsError

            denied = isinstance(e, NoCredentialsError) or (
                isinstance(e, ClientError)
                and e.response.get("Error", {}).get("Code")
                in {"AccessDeniedException", "UnauthorizedException", "AccessDenied"}
            )
            if denied:
                # Credentials/permission won't fix themselves this run.
                _haiku_ocr_disabled = True
    return ""
