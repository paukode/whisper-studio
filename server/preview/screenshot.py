"""Screenshot capture: PNG -> downscaled JPEG -> [WS_PREVIEW_IMAGE] sentinel.

The sentinel is detected in server/tool_executor.py and turned into a real
Anthropic `image` content block in the tool_result, so the model sees actual
pixels — not a base64 blob buried in JSON text.
"""

from __future__ import annotations

import base64
import io
import json

_MAX_LONG_EDGE = 1280
_JPEG_QUALITY = 70

SENTINEL_PREFIX = "[WS_PREVIEW_IMAGE]"


async def take_screenshot(session_id: str, page) -> str:
    """Capture the page, downscale/compress to JPEG, return the sentinel
    string carrying the base64 payload + caption."""
    from PIL import Image

    png_bytes = await page.screenshot(type="png")
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > _MAX_LONG_EDGE:
        scale = _MAX_LONG_EDGE / long_edge
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    jpeg_bytes = buf.getvalue()
    data_b64 = base64.b64encode(jpeg_bytes).decode("ascii")

    payload = {
        "media_type": "image/jpeg",
        "data": data_b64,
        "caption": (
            f"Screenshot of preview session '{session_id}' "
            f"({img.size[0]}x{img.size[1]}, {len(jpeg_bytes)} bytes JPEG)"
        ),
    }
    return f"{SENTINEL_PREFIX}{json.dumps(payload)}"
