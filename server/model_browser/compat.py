"""Pure compatibility logic for the in-app model browser.

No network here — just the rules that decide which Hugging Face GGUF repos the
bundled ``llama-server`` can actually run and how to turn a repo's file list
into a clean per-quant picker. Kept dependency-free so it is trivially unit
tested with no HF calls.

The single most important rule: only surface a model whose GGUF architecture is
one the bundled llama.cpp reliably loads. Offering a model that then fails at
load with "unknown model architecture" after a multi-GB download is the exact
failure this gate exists to prevent, so the allowlist is deliberately
conservative — hide a maybe rather than promise a model we can't serve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Architecture gate ────────────────────────────────────────────────────────
# Architectures the bundled llama.cpp (build >= 10090, see
# server/local/llama_server.py MIN_BUILD) reliably runs. The families the brief
# asks for — Qwen, Gemma, Mistral/Ministral, Llama, Phi — plus a handful of
# equally well-supported ones. Kept conservative on purpose:
#   * brand-new / hybrid-attention archs the current build does NOT load yet
#     (e.g. "qwen3next") are omitted so we never offer a model that fails at
#     load. Bump this set when the bundled engine is upgraded (design P4).
#   * most Mistral / Ministral / Mixtral GGUFs report arch "llama" (the token
#     the converter writes), which is already covered by "llama".
SUPPORTED_ARCHS: frozenset[str] = frozenset(
    {
        "llama",
        "qwen2",
        "qwen3",
        "qwen3moe",
        "gemma",
        "gemma2",
        "gemma3",
        "gemma3n",
        "mistral",
        "phi2",
        "phi3",
        "phi3moe",
        "phimoe",
        "stablelm",
        "starcoder2",
        "cohere",
        "cohere2",
        "granite",
        "granitemoe",
    }
)

# Curated GGUF publishers browsed by default (design §8). "Search all of HF" is
# an explicit opt-in (``all=1``) to keep quality/safety high.
TRUSTED_AUTHORS: tuple[str, ...] = (
    "unsloth",
    "bartowski",
    "lmstudio-community",
    "ggml-org",
    "Qwen",
    "google",
    "mistralai",
)

# Arch prefixes / name hints that mark a chain-of-thought model, so a freshly
# installed model gets supports_thinking on by default. Qwen3 is a thinking
# family; QwQ and DeepSeek-R1 distills are explicit reasoners.
_THINKING_ARCH_PREFIXES = ("qwen3",)
_THINKING_NAME_HINTS = ("thinking", "reasoning", "qwq", "-r1", "deepseek-r")

# ── Memory-fit gate ──────────────────────────────────────────────────────────
# Nothing here is sized for any particular machine — the caller (service.py)
# measures the RUNNING machine's physical memory and companion-model footprint
# at call time and passes those in. These two constants are machine-independent
# rules of thumb applied to whatever RAM the machine actually reports:
#   * Apple's Metal (and most GPU/unified-memory backends) will not let one
#     process claim all of physical RAM before the OS starts reclaiming pages
#     out from under it — recommendedMaxWorkingSetSize on Apple Silicon sits
#     around 3/4 of total unified memory. 0.75 approximates that ceiling on any
#     machine, not just the one this was observed on.
#   * A resident context needs more than the raw weights: KV cache at a large
#     context size plus llama.cpp's compute buffers. 2 GiB is a rough constant
#     rather than a per-model computation (ctx size, layer count, etc. all
#     matter), but it is enough to catch the failure mode this exists for —
#     weights alone leaving no room for anything else.
MEM_BUDGET_FRACTION = 0.75
RUNTIME_OVERHEAD_BYTES = 2 << 30  # 2 GiB

# Second threshold within the budget: a quant that fits but leaves little
# headroom is flagged "tight" rather than a clean "ok", since large-context
# KV growth or another app's memory pressure can tip it into the same failure.
_TIGHT_FRACTION = 0.85


def fit_verdict(
    size_bytes: int | None,
    total_mem_bytes: int | None,
    reserved_bytes: int = 0,
) -> str | None:
    """Whether a quant of ``size_bytes`` can run alongside ``reserved_bytes``
    of other resident models on a machine with ``total_mem_bytes`` of RAM.

    Returns ``'ok'``, ``'tight'``, or ``'too_big'``; ``None`` when either size
    is unknown or non-positive, so the caller can omit the field rather than
    render a guess. ``total_mem_bytes`` and ``reserved_bytes`` describe the
    CALLER's machine — this function hardcodes no machine's numbers, only the
    fractions above.
    """
    if not size_bytes or size_bytes <= 0 or not total_mem_bytes or total_mem_bytes <= 0:
        return None
    budget = MEM_BUDGET_FRACTION * total_mem_bytes - max(0, reserved_bytes)
    needed = size_bytes + RUNTIME_OVERHEAD_BYTES
    if needed > budget:
        return "too_big"
    if needed > _TIGHT_FRACTION * budget:
        return "tight"
    return "ok"


def arch_supported(arch: str | None) -> bool:
    """True when ``arch`` (from the GGUF header) is one the engine can run."""
    return bool(arch) and arch.strip().lower() in SUPPORTED_ARCHS


def is_thinking_model(arch: str | None, repo_id: str, filename: str = "") -> bool:
    """Best-effort guess at whether the model exposes a thinking/reasoning mode.

    Used only for the default ``supports_thinking`` flag on install; the user can
    always correct it in the config editor afterwards.
    """
    a = (arch or "").lower()
    if any(a.startswith(p) for p in _THINKING_ARCH_PREFIXES):
        return True
    hay = f"{repo_id} {filename}".lower()
    return any(h in hay for h in _THINKING_NAME_HINTS)


# ── Filename parsing: quant token, shards, mmproj ────────────────────────────

# The quant code in a GGUF filename: an optional Unsloth "UD-" dynamic prefix
# followed by an IQ*/Q* code (Q4_K_M, Q5_K_S, Q8_0, Q4_0, IQ4_XS, IQ2_M …), or a
# float type (BF16/F16/F32). Anchored on a delimiter so it can't match a stray
# letter inside the model name.
_QUANT_RE = re.compile(
    r"(?:^|[-_.])"
    r"(?P<quant>(?:UD-)?(?:IQ\d+(?:_[A-Za-z0-9]+)*|Q\d+(?:_[A-Za-z0-9]+)*)|BF16|FP16|F16|F32|MXFP4)"
    r"(?=$|[-_.])",
    re.IGNORECASE,
)

# A sharded GGUF: "<base>-00001-of-00003.gguf". The first shard (00001) is the
# one passed to llama-server, which then finds the rest by naming convention.
_SHARD_RE = re.compile(
    r"^(?P<base>.+?)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$",
    re.IGNORECASE,
)


def parse_quant(filename: str) -> str | None:
    """The quant token in a GGUF filename, or None if none is recognizable.

    Takes the LAST match so a repo/model name that happens to contain a
    quant-like token doesn't shadow the real one at the end of the name.
    """
    base = re.sub(r"\.gguf$", "", filename, flags=re.IGNORECASE)
    matches = list(_QUANT_RE.finditer(base))
    if not matches:
        return None
    return matches[-1].group("quant").upper()


def shard_info(filename: str) -> tuple[str, int, int] | None:
    """(logical_base, index, total) for a sharded GGUF, else None.

    ``logical_base`` is the filename with the shard suffix stripped, so every
    shard of one logical model maps to the same base for grouping.
    """
    m = _SHARD_RE.match(filename)
    if not m:
        return None
    return m.group("base"), int(m.group("idx")), int(m.group("total"))


def is_mmproj(filename: str) -> bool:
    """A vision projector (mmproj) file — not a chat model on its own, hidden."""
    return "mmproj" in filename.lower()


def param_size_label(repo_id: str) -> str | None:
    """A human parameter-size label parsed from the repo name (e.g. "0.6B",
    "8B", "30B"), or None. First match wins so "30B-A3B" reads as "30B"."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", repo_id.replace("_", "-"))
    if not m:
        return None
    num = m.group(1)
    return f"{num}B"


@dataclass
class QuantOption:
    """One selectable quant in the repo detail view (shards already folded in)."""

    quant: str
    filename: str  # first shard for a sharded model, else the single file
    size_bytes: int
    is_sharded: bool
    shard_count: int = 1


def group_gguf_files(files: list[tuple[str, int]]) -> list[QuantOption]:
    """Fold a repo's ``[(filename, size_bytes)]`` into per-quant options.

    Rules (design §4/§5):
      * only ``*.gguf`` files;
      * ``*mmproj*`` projector files are dropped (vision, not chat);
      * shards (``-00001-of-000NN.gguf``) collapse to ONE option — sizes summed,
        the first shard kept as the load target;
      * files with no recognizable quant token are skipped (base/unknown dumps).
    One option per (quant); if two files share a quant the larger wins, which
    keeps a single clean row per quant in the picker.
    """
    # base_key -> accumulator. For sharded files the base_key folds all shards.
    grouped: dict[str, dict] = {}
    for filename, size in files:
        if not filename.lower().endswith(".gguf"):
            continue
        if is_mmproj(filename):
            continue
        quant = parse_quant(filename)
        if not quant:
            continue
        shard = shard_info(filename)
        if shard is not None:
            base, idx, total = shard
            key = f"shard::{base}"
            acc = grouped.setdefault(
                key,
                {
                    "quant": quant,
                    "filename": filename,
                    "size": 0,
                    "is_sharded": True,
                    "shard_count": total,
                    "first_idx": idx,
                },
            )
            acc["size"] += int(size or 0)
            # Keep the first shard (00001) as the load target.
            if idx < acc.get("first_idx", 10**9):
                acc["first_idx"] = idx
                acc["filename"] = filename
        else:
            key = f"quant::{quant}"
            acc = grouped.get(key)
            if acc is None or int(size or 0) > acc["size"]:
                grouped[key] = {
                    "quant": quant,
                    "filename": filename,
                    "size": int(size or 0),
                    "is_sharded": False,
                    "shard_count": 1,
                }

    options = [
        QuantOption(
            quant=acc["quant"],
            filename=acc["filename"],
            size_bytes=acc["size"],
            is_sharded=acc["is_sharded"],
            shard_count=acc["shard_count"],
        )
        for acc in grouped.values()
    ]
    # Smallest first: a natural download-size ordering for the picker.
    options.sort(key=lambda o: o.size_bytes)
    return options


# Preference ladder for the pre-selected "recommended" quant (design §5):
# Q4_K_M, then Unsloth's UD-Q4_K_XL, then nearby 4/5-bit K-quants.
_RECOMMENDED_ORDER = (
    "Q4_K_M",
    "UD-Q4_K_XL",
    "Q4_K_S",
    "Q5_K_M",
    "UD-Q5_K_XL",
    "Q5_K_S",
    "Q4_0",
    "UD-Q4_K_M",
)


def recommended_quant(options: list[QuantOption]) -> str | None:
    """The filename to pre-select. Prefers Q4_K_M, then UD-Q4_K_XL, then a nearby
    4/5-bit quant; failing all of those, the median-sized option (avoids picking
    the tiny Q2 or the huge Q8/F16 extremes)."""
    if not options:
        return None
    by_quant = {o.quant.upper(): o for o in options}
    for pref in _RECOMMENDED_ORDER:
        if pref.upper() in by_quant:
            return by_quant[pref.upper()].filename
    ordered = sorted(options, key=lambda o: o.size_bytes)
    return ordered[len(ordered) // 2].filename
