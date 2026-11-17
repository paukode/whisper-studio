"""What a picture costs in the model's window, from its pixel size.

A model bills a picture by the patches it is cut into, not by the length of
its base64 data. The size estimate that decides compaction must count it the
same way: a 1280 x 800 screenshot is about 1,300 tokens, while its data is
more than 100,000 chars.

Claude: one token per 28 x 28 pixel patch. A picture over the long-edge limit
or the token limit is scaled down first.
GPT (the patch models on mantle): one patch per 32 x 32 pixels, times 1.2.
"""

import base64
import binascii
import io
import math
from collections import OrderedDict

CHARS_PER_TOKEN = 4

CLAUDE_PATCH = 28
CLAUDE_MAX_EDGE = 2576
CLAUDE_MAX_TOKENS = 4784

GPT_PATCH = 32
GPT_MULTIPLIER = 1.2
GPT_MAX_PATCHES = 30_000

_CACHE_SIZE = 256
_dimensions: OrderedDict[tuple, tuple[int, int] | None] = OrderedDict()


def _key(data: str) -> tuple:
    return (len(data), data[:48], data[-48:])


def image_dimensions(data: str) -> tuple[int, int] | None:
    """Width and height of a base64 picture, or None when it cannot be read."""
    if not isinstance(data, str) or not data:
        return None
    key = _key(data)
    if key in _dimensions:
        _dimensions.move_to_end(key)
        return _dimensions[key]
    size: tuple[int, int] | None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(data))) as img:
            size = (int(img.width), int(img.height))
        if size[0] <= 0 or size[1] <= 0:
            size = None
    except (binascii.Error, ValueError, OSError, ImportError):
        size = None
    _dimensions[key] = size
    if len(_dimensions) > _CACHE_SIZE:
        _dimensions.popitem(last=False)
    return size


def _patches(width: float, height: float, patch: int) -> int:
    return math.ceil(width / patch) * math.ceil(height / patch)


def claude_image_tokens(width: int, height: int) -> int:
    scale = min(1.0, CLAUDE_MAX_EDGE / max(width, height))
    w, h = width * scale, height * scale
    tokens = _patches(w, h, CLAUDE_PATCH)
    if tokens > CLAUDE_MAX_TOKENS:
        shrink = math.sqrt(CLAUDE_MAX_TOKENS / tokens)
        tokens = min(CLAUDE_MAX_TOKENS, _patches(w * shrink, h * shrink, CLAUDE_PATCH))
    return tokens


def gpt_image_tokens(width: int, height: int) -> int:
    patches = min(GPT_MAX_PATCHES, _patches(width, height, GPT_PATCH))
    return math.ceil(patches * GPT_MULTIPLIER)


def image_tokens(data: str, openai: bool = False) -> int | None:
    """The tokens one base64 picture costs, or None when it cannot be read."""
    size = image_dimensions(data)
    if size is None:
        return None
    return gpt_image_tokens(*size) if openai else claude_image_tokens(*size)


def image_chars(data: str, openai: bool = False) -> int:
    """A picture's weight on the 4-chars-per-token scale of the size estimate.
    Data that is not a readable picture counts at its length, as before."""
    tokens = image_tokens(data, openai)
    if tokens is None:
        return len(data) if isinstance(data, str) else 0
    return tokens * CHARS_PER_TOKEN
