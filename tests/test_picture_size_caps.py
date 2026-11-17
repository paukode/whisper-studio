"""Pictures sent to the model are made smaller once, to the long-edge cap."""

import asyncio
import base64
import io
import json

from PIL import Image

from server import attachments
from server.chat import image_size
from server.preview import screenshot


def png(width, height) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def test_an_attached_picture_over_the_cap_is_scaled_to_it():
    data, _media = attachments._prepare_image(png(3000, 1500), "image/png")
    width, height = size_of(data)
    assert max(width, height) == attachments.IMAGE_MAX_DIM
    assert abs(width / height - 2) < 0.01


def test_an_attached_picture_under_the_cap_is_not_changed():
    original = png(400, 300)
    data, media = attachments._prepare_image(original, "image/png")
    assert data == original and media == "image/png"


def test_a_screenshot_is_scaled_to_its_cap():
    class Page:
        async def screenshot(self, type):  # noqa: A002 - the Playwright argument name
            return png(1280, 800)

    sentinel = asyncio.run(screenshot.take_screenshot("s", Page()))
    payload = json.loads(sentinel[sentinel.index("{") :])
    width, height = size_of(base64.b64decode(payload["data"]))
    assert max(width, height) == screenshot._MAX_LONG_EDGE


def test_the_caps_cut_the_cost_of_a_picture_at_the_old_size():
    for rule in (image_size.claude_image_tokens, image_size.gpt_image_tokens):
        assert rule(attachments.IMAGE_MAX_DIM, 543) < rule(1568, 724) * 0.65
        assert rule(screenshot._MAX_LONG_EDGE, 600) < rule(1280, 800) * 0.65
