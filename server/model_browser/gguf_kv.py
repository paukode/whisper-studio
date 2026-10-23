"""KV-cache size of a GGUF model at a context length, read from its header.

llama-server allocates the whole KV cache when it loads a model: one K row and
one V row per context token for every attention layer, in f16 (the app passes
no --cache-type flags). At the 32K context local chat runs with, that is often
close to the size of the weights: Ministral 3 14B keeps 5 GiB of KV cache next
to 7.8 GiB of weights. A flat allowance cannot tell that model from a hybrid
one that keeps a fifth of it, so the memory-fit grade reads the shape here.

Pure: :func:`read_metadata` parses any binary file object, so the caller picks
the bytes (a local file, or a ranged read on Hugging Face). The size follows
llama.cpp's own layout (one slot, no --swa-full):
  * a full-attention layer keeps every token of the context;
  * a sliding-window layer keeps the window plus one micro-batch;
  * a recurrent layer of a hybrid model keeps a small fixed state and no KV
    rows (``full_attention_interval``, or a per-layer head_count_kv of 0);
  * the last ``shared_kv_layers`` layers reuse earlier layers' cache.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO

_MAGIC = b"GGUF"
_STRING, _ARRAY = 8, 9
_SCALARS = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}

# Per-layer arrays are at most a few hundred items. Anything longer is the
# tokenizer (vocabulary, scores, merges): a numeric one is skipped with a seek,
# and a string one ends the read, because its bytes cannot be skipped without
# reading them and the converter writes every model parameter before it.
# That keeps a remote read to a few KB instead of the 8 to 16 MB vocabulary.
_LONG_ARRAY = 1024
# Corrupt-file guards: no real header has more keys or a longer string.
_MAX_KV = 100_000
_MAX_STRING = 16 << 20

# llama-server defaults the app does not override (server/local/llama_server.py
# passes --ctx-size and --parallel 1 only): f16 K and V, a 512-token
# micro-batch, and cache sizes padded to 256 cells.
_KV_ELEMENT_BYTES = 2
_N_UBATCH = 512
_CELL_PAD = 256

# Architectures whose sliding-window layout llama.cpp hard-codes instead of
# reading it from the header: of every N layers, the first N-1 use the window
# and the last one attends to the full context.
_SWA_PERIOD = {"gemma2": 2, "gemma3": 6, "cohere2": 4}


class GgufError(ValueError):
    """The bytes are not a GGUF header this reader understands."""


def _unpack(f: BinaryIO, fmt: str) -> tuple:
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise GgufError("GGUF header ends early")
    return struct.unpack(fmt, data)


def _string(f: BinaryIO) -> str:
    (n,) = _unpack(f, "<Q")
    if n > _MAX_STRING:
        raise GgufError(f"GGUF string of {n} bytes")
    data = f.read(n)
    if len(data) != n:
        raise GgufError("GGUF header ends early")
    return data.decode("utf-8", "replace")


def _value(f: BinaryIO, vtype: int) -> Any:
    if vtype == _STRING:
        return _string(f)
    if vtype == _ARRAY:
        etype, n = _unpack(f, "<IQ")
        if n > _LONG_ARRAY:
            raise GgufError("long nested GGUF array")
        return [_value(f, etype) for _ in range(n)]
    fmt = _SCALARS.get(vtype)
    if fmt is None:
        raise GgufError(f"unknown GGUF value type {vtype}")
    return _unpack(f, fmt)[0]


def read_metadata(f: BinaryIO) -> dict[str, Any]:
    """The key/values of a GGUF header, up to the tokenizer vocabulary.

    Raises GgufError on bytes that are not a GGUF v2 or v3 header."""
    if f.read(4) != _MAGIC:
        raise GgufError("not a GGUF file")
    (version,) = _unpack(f, "<I")
    if version < 2:
        raise GgufError(f"GGUF version {version} is not supported")
    _n_tensors, n_kv = _unpack(f, "<QQ")
    if n_kv > _MAX_KV:
        raise GgufError(f"GGUF header with {n_kv} keys")
    meta: dict[str, Any] = {}
    for _ in range(n_kv):
        key = _string(f)
        (vtype,) = _unpack(f, "<I")
        if vtype != _ARRAY:
            meta[key] = _value(f, vtype)
            continue
        etype, n = _unpack(f, "<IQ")
        if n <= _LONG_ARRAY:
            meta[key] = [_value(f, etype) for _ in range(n)]
        elif etype in _SCALARS:
            f.seek(n * struct.calcsize(_SCALARS[etype]), io.SEEK_CUR)
        else:
            break
    return meta


@dataclass(frozen=True)
class KvShape:
    """KV bytes per context token, split by how many tokens a layer keeps."""

    full_bytes_per_token: int  # layers that keep the whole context
    window_bytes_per_token: int  # sliding-window layers
    window: int  # their window in tokens, 0 when the model has none


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _per_layer(value: Any, n_layer: int) -> list[int] | None:
    """A per-layer count from a scalar or a per-layer array, else None."""
    if _is_count(value):
        return [value] * n_layer
    if isinstance(value, list) and len(value) == n_layer and all(map(_is_count, value)):
        return list(value)
    return None


def _window_layers(arch: str, meta: dict[str, Any], n_layer: int) -> list[bool]:
    """Which layers use the sliding window. A model whose layout is unknown
    counts as full attention everywhere, which can only overstate its need."""
    pattern = meta.get(f"{arch}.attention.sliding_window_pattern")
    if isinstance(pattern, list) and len(pattern) == n_layer:
        return [bool(p) for p in pattern]
    period = pattern if _is_count(pattern) and pattern > 1 else _SWA_PERIOD.get(arch)
    if period:
        return [il % period < period - 1 for il in range(n_layer)]
    return [False] * n_layer


def kv_shape(meta: dict[str, Any]) -> KvShape | None:
    """The KV layout from header key/values, or None when the header lacks
    the attention keys (the caller then keeps its flat allowance)."""
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None

    def get(name: str) -> Any:
        return meta.get(f"{arch}.{name}")

    n_layer = get("block_count")
    if not _is_count(n_layer) or n_layer == 0:
        return None
    heads = _per_layer(get("attention.head_count"), n_layer)
    # No head_count_kv means no grouped-query attention, as in llama.cpp.
    raw_kv = get("attention.head_count_kv")
    heads_kv = heads if raw_kv is None else _per_layer(raw_kv, n_layer)
    if heads_kv is None:
        return None

    embd = get("embedding_length")
    default_dim = embd // heads[0] if _is_count(embd) and heads and heads[0] else None
    k_dim = get("attention.key_length") or default_dim
    v_dim = get("attention.value_length") or k_dim
    if not _is_count(k_dim) or not _is_count(v_dim):
        return None
    k_dim_swa = get("attention.key_length_swa") or k_dim
    v_dim_swa = get("attention.value_length_swa") or v_dim

    window = get("attention.sliding_window")
    window = window if _is_count(window) else 0
    swa = _window_layers(arch, meta, n_layer) if window else [False] * n_layer

    has_kv = [h > 0 for h in heads_kv]
    interval = get("full_attention_interval")
    if _is_count(interval) and interval > 1:
        has_kv = [ok and (il + 1) % interval == 0 for il, ok in enumerate(has_kv)]
    shared = get("attention.shared_kv_layers")
    if _is_count(shared) and 0 < shared < n_layer:
        has_kv = [ok and il < n_layer - shared for il, ok in enumerate(has_kv)]

    full = windowed = 0
    for il in range(n_layer):
        if not has_kv[il]:
            continue
        if swa[il]:
            windowed += heads_kv[il] * (k_dim_swa + v_dim_swa) * _KV_ELEMENT_BYTES
        else:
            full += heads_kv[il] * (k_dim + v_dim) * _KV_ELEMENT_BYTES
    return KvShape(full, windowed, window if windowed else 0)


def _pad(n: int) -> int:
    return -(-n // _CELL_PAD) * _CELL_PAD


def kv_cache_bytes(shape: KvShape, n_ctx: int) -> int:
    """Bytes llama-server allocates for the KV cache of one slot at ``n_ctx``."""
    cells = _pad(max(1, int(n_ctx)))
    window_cells = min(cells, _pad(shape.window + _N_UBATCH)) if shape.window else cells
    return shape.full_bytes_per_token * cells + shape.window_bytes_per_token * window_cells
