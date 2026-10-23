"""Build GGUF headers in memory for tests of the KV-cache estimate.

A minimal GGUF v3 writer (key/values only, no tensors) and the key/values of
a plain full-attention model, so a test states the shape it needs and never
depends on a real model file.
"""

from __future__ import annotations

import struct


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def _typed(value) -> tuple[int, bytes]:
    if isinstance(value, bool):
        return 7, struct.pack("<?", value)
    if isinstance(value, int):
        return 4, struct.pack("<I", value)
    if isinstance(value, float):
        return 6, struct.pack("<f", value)
    if isinstance(value, str):
        return 8, _str(value)
    if isinstance(value, list):
        etype = _typed(value[0])[0]
        body = b"".join(_typed(v)[1] for v in value)
        return 9, struct.pack("<IQ", etype, len(value)) + body
    raise TypeError(value)


def gguf_bytes(meta: dict) -> bytes:
    """A GGUF v3 header (no tensors) with ``meta`` in insertion order."""
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta))
    for key, value in meta.items():
        vtype, body = _typed(value)
        out += _str(key) + struct.pack("<I", vtype) + body
    return out


_ATTENTION_KEYS = {"sliding_window", "sliding_window_pattern", "shared_kv_layers"}


def header(arch="llama", layers=40, heads=32, kv_heads=8, head_dim=128, **extra) -> dict:
    """Header key/values of a plain full-attention model, plus ``extra`` keys
    (named without the arch prefix, and without ``attention.`` for the
    window and shared-layer keys)."""
    extra = {(f"attention.{k}" if k in _ATTENTION_KEYS else k): v for k, v in extra.items()}
    meta = {
        "general.architecture": arch,
        f"{arch}.block_count": layers,
        f"{arch}.embedding_length": heads * head_dim,
        f"{arch}.attention.head_count": heads,
        f"{arch}.attention.head_count_kv": kv_heads,
        f"{arch}.attention.key_length": head_dim,
        f"{arch}.attention.value_length": head_dim,
    }
    meta.update({f"{arch}.{k}": v for k, v in extra.items()})
    return meta


def write_gguf(path, meta: dict, size: int = 0) -> str:
    """Write a header-only GGUF file at ``path``, padded to ``size`` bytes
    with a sparse tail so the file reports a weights size without using it."""
    data = gguf_bytes(meta)
    with open(path, "wb") as f:
        f.write(data)
        if size > len(data):
            f.truncate(size)
    return str(path)
