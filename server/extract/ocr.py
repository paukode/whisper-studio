"""OCR for scanned PDFs and text-heavy images.

Engine selection, in order:
  1. Apple Vision (native macOS OCR via the ``ocrmac`` package). The default,
     always tried first. Zero model download, ~0 RAM, fast on printed text,
     fully on-device and private. No Bedrock cost.
  2. Bedrock Claude Haiku vision, only as a fallback when Apple Vision fails or
     is unavailable AND AWS credentials resolve.

We never call Bedrock without working credentials, and never before Apple
Vision has had a chance, so the normal path is fully on-device.
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
    resp = client.invoke_model(modelId=model_id, body=body)
    payload = json.loads(resp["body"].read())
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


def ocr_images(images) -> str:
    """OCR a list of PIL images into Markdown text.

    Apple Vision (native, on-device) runs first and is the default. Haiku is
    only tried as a fallback when Apple Vision fails or is unavailable, and only
    when AWS creds resolve. A successful-but-empty Apple Vision pass also falls
    through to Haiku deliberately: Apple Vision frequently returns nothing for
    images that do contain text, so Haiku is a second reader (see
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
