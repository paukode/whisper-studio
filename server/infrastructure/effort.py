"""Per-model effort-level catalogue and label↔API mapping.

Single source of truth for which effort levels each model exposes and how
each friendly label maps to the Bedrock ``output_config.effort`` value.

Effort is the adaptive-reasoning depth knob. On Bedrock the request carries
``thinking: {"type": "adaptive"}`` plus ``output_config: {"effort": <api>}``.
Models without effort support (Haiku) send neither — ``thinking`` is omitted.

``ultracode`` is a session MODE, not a raw effort value: it sends ``xhigh``
*and* turns on dynamic-workflow orchestration (see
``build_system_prompt(ultracode=...)``). Mirrors Claude Code's own semantics.
"""

from __future__ import annotations

import re

# Friendly labels in intensity order (low → highest).
EFFORT_ORDER = ["none", "low", "medium", "high", "extra", "max", "ultracode"]

# Which labels each tier exposes.
EFFORT_TIERS = {
    "full": ["low", "medium", "high", "extra", "max", "ultracode"],
    "standard": ["low", "medium", "high", "max"],
    # OpenAI-on-Bedrock (GPT-5.x). The label "max" maps to the model's top
    # reasoning tier in openai_bedrock.runtime: "xhigh" for GPT-5.5/5.4 (their
    # ladder tops there; "minimal" rejected — verified live), the real "max"
    # tier for GPT-5.6 (verified live 2026-07-15).
    # "ultracode" is a session MODE (orchestration directive + workflow
    # tooling); on this tier the wire value maps to the model's top
    # reasoning tier via openai_bedrock.runtime.reasoning_effort_for.
    "openai": ["none", "low", "medium", "high", "max", "ultracode"],
    "none": [],
}

# Friendly label → Bedrock output_config.effort value. Extra and Ultracode
# both send xhigh; Ultracode additionally orchestrates (handled in the prompt).
_LABEL_TO_API = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
    "ultracode": "xhigh",
}

DEFAULT_EFFORT = "high"

_OPUS_RE = re.compile(r"opus(\d+)\.(\d+)$", re.IGNORECASE)


def infer_effort_tier(key: str) -> str:
    """Infer a model's effort tier from its config key when not declared.

    full     — Opus ≥ 4.8 and Fable (low…ultracode)
    standard — Sonnet and Opus 4.0–4.7 (low/medium/high/max)
    none     — Haiku (no effort/thinking)
    """
    k = key.lower()
    if "haiku" in k:
        return "none"
    if "fable" in k:
        return "full"
    m = _OPUS_RE.match(k)
    if m and (int(m.group(1)), int(m.group(2))) >= (4, 8):
        return "full"
    return "standard"


def effort_levels_for(meta: dict | None, key: str) -> list[str]:
    """Allowed effort labels for a model. An explicit ``effort_tier`` in the
    model metadata wins; otherwise the tier is inferred from the key."""
    tier = (meta or {}).get("effort_tier") or infer_effort_tier(key)
    return list(EFFORT_TIERS.get(tier, EFFORT_TIERS["standard"]))


def clamp_effort(level: str, allowed: list[str]) -> str | None:
    """Clamp ``level`` to the nearest allowed label at or below it (matching
    Claude Code's own fallback rule). Returns ``None`` when the model has no
    effort support (empty ``allowed``)."""
    if not allowed:
        return None
    if level in allowed:
        return level
    try:
        ci = EFFORT_ORDER.index(level)
    except ValueError:
        ci = EFFORT_ORDER.index(DEFAULT_EFFORT)
    best: str | None = None
    for lv in allowed:
        i = EFFORT_ORDER.index(lv)
        if i <= ci and (best is None or i > EFFORT_ORDER.index(best)):
            best = lv
    return best or allowed[0]


def default_effort_for(meta: dict | None, key: str) -> str:
    """The default effort label for a model (DEFAULT_EFFORT, clamped to what
    the model supports). Falls back to DEFAULT_EFFORT for effort-less models."""
    allowed = effort_levels_for(meta, key)
    return clamp_effort(DEFAULT_EFFORT, allowed) or DEFAULT_EFFORT


def api_effort(level: str) -> str:
    """Map a friendly label to the Bedrock ``output_config.effort`` value."""
    return _LABEL_TO_API.get(level, "high")


def is_ultracode(level: str | None) -> bool:
    return level == "ultracode"


def normalize_effort(level: str | None) -> str:
    """Map legacy/unknown values (e.g. the retired ``'auto'``) onto a known
    label so downstream code never sees a stale value."""
    return level if level in EFFORT_ORDER else DEFAULT_EFFORT
