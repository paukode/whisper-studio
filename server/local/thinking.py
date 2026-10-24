"""Thinking on local models: what a model's own chat template supports, and
what a request sends at each thinking level.

A local model gets the same level chip as a cloud model: off (``none``),
``low``, ``medium``, ``high`` and ``max`` (the default). The chat template
decides what a level means:

* a template that reads ``reasoning_effort`` (gpt-oss) gets the level itself;
  ``max`` sends its top level, ``high``;
* any other thinking template gets ``enable_thinking``, and on llama-server a
  thinking-token cap (``thinking_budget_tokens``, llama.cpp b11851): the server
  closes the thinking block when the cap is reached. ``max`` has no cap.
  mlx-lm has no such cap, so an MLX model of this kind offers off and max.

A request also allows the thinking on top of the model's answer cap
(``max_output``), so thinking never takes the answer's tokens.

Measured 2026-10-24 with llama.cpp b11851 on one reasoning question: Qwen3.5 9B
thought 0, 228, 1,581 and 5,193 characters with thinking off, a 64-token cap,
a 512-token cap and no cap (2.4 s to 79 s), with the same correct answer.
"""

from __future__ import annotations

import functools
import json
import logging
import mmap
import os
import struct
from dataclasses import dataclass

log = logging.getLogger("whisper-studio")

LEVELS = ("none", "low", "medium", "high", "max")
DEFAULT_LEVEL = "max"
# Thinking-token caps per level; max has none.
BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}
# The room a max-level request leaves for thinking before the answer.
MAX_THINKING_TOKENS = 32768

# Template text that shows a thinking mode: the switches templates read, and
# the tags they write around the thinking.
_MARKERS = (
    "enable_thinking",
    "reasoning_effort",
    "reasoning_content",
    "<think>",
    "<|think|>",
    "[THINK]",
    "<thinking>",
)


@dataclass(frozen=True)
class Caps:
    can_think: bool
    # The template reads reasoning_effort, so it takes the level itself.
    native_levels: bool


NO_THINKING = Caps(False, False)


def caps_from_template(template: str | None) -> Caps:
    if not template:
        return NO_THINKING
    native = "reasoning_effort" in template
    return Caps(native or any(m in template for m in _MARKERS), native)


# ── Reading the chat template ────────────────────────────────────────────────

_STRING, _ARRAY = 8, 9
_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_TEMPLATE_KEY = b"tokenizer.chat_template"


def gguf_chat_template(path: str) -> str | None:
    """``tokenizer.chat_template`` from a local GGUF file. The key usually
    follows the vocabulary, so every key is walked (a string array by its
    item lengths) over a memory map: only header pages are read."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        if mm[:4] != b"GGUF" or struct.unpack_from("<I", mm, 4)[0] < 2:
            return None
        (n_kv,) = struct.unpack_from("<Q", mm, 16)
        off = 24

        def string(at: int) -> tuple[int, bytes]:
            (n,) = struct.unpack_from("<Q", mm, at)
            return at + 8 + n, mm[at + 8 : at + 8 + n]

        for _ in range(n_kv):
            off, key = string(off)
            (vtype,) = struct.unpack_from("<I", mm, off)
            off += 4
            if vtype == _STRING:
                off, value = string(off)
                if key == _TEMPLATE_KEY:
                    return value.decode("utf-8", "replace")
            elif vtype == _ARRAY:
                etype, n = struct.unpack_from("<IQ", mm, off)
                off += 12
                if etype == _STRING:
                    for _ in range(n):
                        off += 8 + struct.unpack_from("<Q", mm, off)[0]
                else:
                    off += n * _SCALAR_SIZES[etype]
            else:
                off += _SCALAR_SIZES[vtype]
    return None


def mlx_chat_template(model_dir: str) -> str | None:
    """The chat template of an MLX snapshot, from the files mlx-lm reads:
    ``chat_template.jinja``, else ``chat_template.json`` or
    ``tokenizer_config.json`` (a string, or a list of named templates)."""
    jinja = os.path.join(model_dir, "chat_template.jinja")
    if os.path.isfile(jinja):
        with open(jinja, encoding="utf-8") as f:
            return f.read()
    for name in ("chat_template.json", "tokenizer_config.json"):
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            template = json.load(f).get("chat_template")
        if isinstance(template, str):
            return template
        if isinstance(template, list):
            return "\n".join(t.get("template", "") for t in template if isinstance(t, dict))
    return None


@functools.lru_cache(maxsize=64)
def _caps_at(path: str, mtime_ns: int, mlx: bool) -> Caps:
    return caps_from_template(mlx_chat_template(path) if mlx else gguf_chat_template(path))


def caps_for(key: str) -> Caps:
    """What the downloaded model ``key`` supports; no thinking when it is not
    a local model, not downloaded, or its template cannot be read."""
    from server.local import runtime

    try:
        if not runtime.is_local_model(key) or not runtime.is_downloaded(key):
            return NO_THINKING
        mlx = runtime.is_mlx_model(key)
        path = runtime.mlx_dir(key) if mlx else runtime.gguf_path(key)
        return _caps_at(path, os.stat(path).st_mtime_ns, mlx)
    except Exception as e:  # noqa: BLE001 - an unreadable model is a model without thinking
        log.warning("Could not read the chat template of %s: %s", key, e)
        return NO_THINKING


# ── Levels and requests ──────────────────────────────────────────────────────


def levels_for(key: str) -> list[str]:
    """The thinking levels the composer offers for ``key``."""
    from server.local import runtime

    caps = caps_for(key)
    if not caps.can_think:
        return []
    if caps.native_levels or not runtime.is_mlx_model(key):
        return list(LEVELS)
    return ["none", "max"]


def request_fields(level: str | None, caps: Caps, *, mlx: bool, answer_tokens: int) -> dict:
    """The body fields of one chat request at ``level``: ``max_tokens`` and the
    thinking switches. ``level`` None means the model does not think."""
    if level is None or not caps.can_think:
        return {"max_tokens": answer_tokens}
    if level == "none":
        return {"max_tokens": answer_tokens, **off_fields(mlx=mlx)}
    kwargs: dict = {"enable_thinking": True}
    fields: dict = {}
    if caps.native_levels:
        effort = "high" if level == "max" else level
        if mlx:
            kwargs["reasoning_effort"] = effort
        else:
            fields["reasoning_effort"] = effort
    elif not mlx and level in BUDGETS:
        fields["thinking_budget_tokens"] = BUDGETS[level]
    room = BUDGETS.get(level, MAX_THINKING_TOKENS)
    return {"max_tokens": answer_tokens + room, "chat_template_kwargs": kwargs, **fields}


def fields_for(key: str, level: str | None, answer_tokens: int) -> dict:
    """:func:`request_fields` for the model ``key``: its template and engine."""
    caps = caps_for(key)
    if not caps.can_think:
        return {"max_tokens": answer_tokens}
    from server.local import runtime

    return request_fields(level, caps, mlx=runtime.is_mlx_model(key), answer_tokens=answer_tokens)


def off_fields(*, mlx: bool) -> dict:
    """The fields that turn thinking off. llama-server reads
    ``reasoning_effort: none`` for every template; mlx-lm only passes the
    template switch through."""
    fields: dict = {"chat_template_kwargs": {"enable_thinking": False}}
    if not mlx:
        fields["reasoning_effort"] = "none"
    return fields
