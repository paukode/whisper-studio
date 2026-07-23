"""Image OCR helper.

Pulls readable text out of an uploaded image so text-only chat models (the local
Gemma build) get something to work with. Vision models still receive the raw
image block alongside this.
"""

import io
import logging

log = logging.getLogger("whisper-studio")


def ocr_image_bytes(data: bytes) -> str:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        log.warning("image open for OCR failed: %s", e)
        return ""

    from server.extract.ocr import ocr_images

    return ocr_images([img])
