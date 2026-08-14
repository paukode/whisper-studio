"""Per-model effort-level catalogue and label↔API mapping.

Single source of truth for which effort levels each model exposes and how
each friendly label maps to the Bedrock ``output_config.effort`` value.

Effort is the adaptive-reasoning depth knob. On Bedrock the request carries
``thinking: {"type": "adaptive"}`` plus ``output_config: {"effort": <api>}``.
Models without effort support (Haiku) send neither — ``thinking`` is omitted.

``ultracode`` is a session MODE, not a raw effort value: it turns on
dynamic-workflow orchestration (see ``build_system_prompt(ultracode=...)``) on
top of the model's deepest reasoning setting. Mirrors Claude Code's own
semantics.

Two independent properties decide what a model offers, and they are kept
separate on purpose:

* the **reasoning ladder** (``effort_tier``) — which raw ``output_config.effort``
  values the provider will actually accept for this model;
* **orchestration support** (``supports_ultracode``) — whether the model is
  capable of authoring and driving a workflow script.

Welding the two together is what used to keep Sonnet 5 off ultracode: its
ladder has no ``xhigh`` rung, and ultracode was hardcoded to send ``xhigh``.
Ultracode now rides whatever the model's own top rung is, so a model can
orchestrate without pretending to accept a reasoning value it does not have.
"""

from __future__ import annotations

import re

# Friendly labels in intensity order (low → highest).
EFFORT_ORDER = ["none", "low", "medium", "high", "extra", "max", "ultracode"]

# The raw reasoning ladder each tier exposes, WITHOUT ultracode — that rung is
# appended per model by :func:`effort_levels_for` from the orchestration
# capability, not from the tier.
EFFORT_TIERS = {
    "full": ["low", "medium", "high", "extra", "max"],
    "standard": ["low", "medium", "high", "max"],
    # OpenAI-on-Bedrock (GPT-5.x). The label "max" maps to the model's top
    # reasoning tier in openai_bedrock.runtime: "xhigh" for GPT-5.5/5.4 (their
    # ladder tops there; "minimal" rejected — verified live), the real "max"
    # tier for GPT-5.6 (verified live 2026-07-15).
    "openai": ["none", "low", "medium", "high", "max"],
    "none": [],
}

# Tiers whose models can orchestrate a workflow unless config says otherwise.
# A model outside these can still opt in with an explicit ``supports_ultracode``
# in its config entry — that is how Sonnet 5 gets ultracode on a standard ladder.
_ULTRACODE_TIERS = {"full", "openai"}

# Friendly label → Bedrock output_config.effort value. "ultracode" has no fixed
# entry: it resolves per model to that model's top rung (see api_effort_for).
_LABEL_TO_API = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
    # Kept for callers with no model in hand. Model-aware callers should use
    # api_effort_for, which sends the model's real top rung instead.
    "ultracode": "xhigh",
}

# Aliases accepted from places that speak the raw wire vocabulary rather than
# the app's labels — notably workflow scripts, whose documented `effort` option
# used the Claude Code names. Mapped onto app labels so one vocabulary reaches
# the providers.
_LABEL_ALIASES = {"xhigh": "extra", "minimal": "low", "none": "none"}

DEFAULT_EFFORT = "high"

_OPUS_RE = re.compile(r"opus(\d+)\.(\d+)$", re.IGNORECASE)
# Both the config key ("sonnet5", "sonnet5.0") and the Bedrock model id
# ("global.anthropic.claude-sonnet-5") name their version; either is enough.
_SONNET_KEY_RE = re.compile(r"sonnet(\d+)", re.IGNORECASE)
_SONNET_ID_RE = re.compile(r"claude-sonnet-(\d+)", re.IGNORECASE)
_OPUS_ID_RE = re.compile(r"claude-opus-(\d+)", re.IGNORECASE)


def infer_effort_tier(key: str) -> str:
    """Infer a model's raw reasoning ladder from its config key when the entry
    does not declare one.

    full     — Opus ≥ 4.8 and Fable (low…max, including the xhigh rung)
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


def effort_tier_for(meta: dict | None, key: str) -> str:
    """This model's raw reasoning ladder name. Explicit config wins."""
    return (meta or {}).get("effort_tier") or infer_effort_tier(key)


def infer_supports_ultracode(meta: dict | None, key: str) -> bool:
    """Whether a model can drive workflow orchestration, when its config entry
    does not say.

    Every model on an orchestration-capable ladder qualifies, plus Sonnet 5 and
    up, whose reasoning ladder is narrower but which orchestrates perfectly
    well. Version is read from the config key OR the Bedrock model id, so a
    renamed entry ("my-sonnet5", "claude-sonnet-5") keeps its capability instead
    of quietly losing it to key-spelling.
    """
    tier = effort_tier_for(meta, key)
    if not EFFORT_TIERS.get(tier, EFFORT_TIERS["standard"]):
        return False  # no effort at all (Haiku) ⇒ nothing to orchestrate with
    if tier in _ULTRACODE_TIERS:
        return True
    model_id = str((meta or {}).get("id") or "")
    for pattern, text in (
        (_SONNET_KEY_RE, key),
        (_SONNET_ID_RE, model_id),
        (_OPUS_ID_RE, model_id),
    ):
        m = pattern.search(text or "")
        if m and int(m.group(1)) >= 5:
            return True
    return False


def supports_ultracode(meta: dict | None, key: str) -> bool:
    """Whether this model offers the ultracode level. An explicit
    ``supports_ultracode`` in the model metadata wins; otherwise inferred."""
    declared = (meta or {}).get("supports_ultracode")
    if declared is not None:
        return bool(declared)
    return infer_supports_ultracode(meta, key)


def effort_levels_for(meta: dict | None, key: str) -> list[str]:
    """Allowed effort labels for a model: its raw ladder, plus ``ultracode``
    when the model can orchestrate."""
    tier = effort_tier_for(meta, key)
    levels = list(EFFORT_TIERS.get(tier, EFFORT_TIERS["standard"]))
    if levels and supports_ultracode(meta, key):
        levels.append("ultracode")
    return levels


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


def resolve_effort(meta: dict | None, key: str, requested: str | None) -> str | None:
    """The one call every entry point should make: normalize a requested label
    and clamp it to what THIS model supports.

    ``None`` means the model has no effort support and must send neither
    ``thinking`` nor ``output_config`` (Haiku). Chat turns, subagents, workflow
    children, cron and headless runs all resolve through here so a session's
    effort survives every hop at the highest level each model can honour.
    """
    return clamp_effort(normalize_effort(requested), effort_levels_for(meta, key))


def default_effort_for(meta: dict | None, key: str) -> str:
    """The default effort label for a model (DEFAULT_EFFORT, clamped to what
    the model supports). Falls back to DEFAULT_EFFORT for effort-less models."""
    allowed = effort_levels_for(meta, key)
    return clamp_effort(DEFAULT_EFFORT, allowed) or DEFAULT_EFFORT


def api_effort(level: str) -> str:
    """Map a friendly label to the Bedrock ``output_config.effort`` value.

    Model-unaware: ``ultracode`` sends the ``xhigh`` rung. Callers that know
    which model they are talking to should use :func:`api_effort_for`, which
    sends that model's real top rung instead.
    """
    return _LABEL_TO_API.get(level, "high")


def api_effort_for(level: str, meta: dict | None, key: str) -> str:
    """Model-aware :func:`api_effort`.

    ``ultracode`` is an orchestration mode, not a wire value, so it rides the
    deepest reasoning rung the model actually has: ``xhigh`` where the ladder
    includes it (Opus 4.8+, Fable), otherwise the ladder's own top — ``max`` for
    Sonnet 5. Sending ``xhigh`` to a model without that rung is exactly the
    error this avoids.
    """
    if level != "ultracode":
        return api_effort(level)
    ladder = [lv for lv in EFFORT_TIERS.get(effort_tier_for(meta, key), []) if lv != "none"]
    if not ladder:
        return api_effort(level)
    top = "extra" if "extra" in ladder else ladder[-1]
    return _LABEL_TO_API.get(top, "high")


def is_ultracode(level: str | None) -> bool:
    return level == "ultracode"


def known_effort_label(value: str | None) -> str | None:
    """The app label for a name a caller supplied, or ``None`` if it is not a
    label or a known raw alias.

    Unlike :func:`normalize_effort` this does NOT coerce garbage to the default
    — callers that have a better fallback than "high" (an inherited level, say)
    need to tell "the caller asked for something else" apart from "the caller
    asked for nonsense"."""
    v = str(value or "").strip().lower()
    if v in EFFORT_ORDER:
        return v
    return _LABEL_ALIASES.get(v)


def normalize_effort(level: str | None) -> str:
    """Map legacy/unknown values (e.g. the retired ``'auto'``) and raw wire
    aliases (``'xhigh'``) onto a known app label so downstream code never sees
    a stale or provider-specific value."""
    if level in EFFORT_ORDER:
        return level
    return _LABEL_ALIASES.get(str(level or "").lower(), DEFAULT_EFFORT)
