"""Per-model effort-level catalogue and label↔API mapping.

Single source of truth for which effort levels each model exposes and how
each friendly label maps to the Bedrock ``output_config.effort`` value.

Effort is the adaptive-reasoning depth knob. On Bedrock the request carries
``thinking: {"type": "adaptive"}`` plus ``output_config: {"effort": <api>}``.
Models without effort support (Haiku up to 4.5) send neither — ``thinking`` is omitted.

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

Ultracode rides whatever the model's own top rung is, so a model can
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
    # OpenAI-on-Bedrock (GPT-6). GPT-6.2 Astra and Sol have no "none" rung:
    # bedrock-mantle rejects reasoning.effort "none" for them with
    # unsupported_value (verified live 2026-11-24; accepted values are
    # low/medium/high/xhigh/max). Every GPT id gets this ladder; "none" was
    # never probed on GPT-6.1 Terra or GPT-6 Luna.
    "openai6": ["low", "medium", "high", "max"],
    "none": [],
}

# Tiers whose models can orchestrate a workflow unless config says otherwise.
# A model outside these can still opt in with an explicit ``supports_ultracode``
# in its config entry.
_ULTRACODE_TIERS = {"full", "openai6"}

# Friendly label → Bedrock output_config.effort value. "ultracode" has no fixed
# entry: it resolves per model to that model's top rung (see api_effort_for).
_LABEL_TO_API = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
    "max": "max",
}

# Aliases accepted from places that speak the raw wire vocabulary rather than
# the app's labels — notably workflow scripts, whose documented `effort` option
# used the Claude Code names. Mapped onto app labels so one vocabulary reaches
# the providers.
_LABEL_ALIASES = {"xhigh": "extra", "minimal": "low", "none": "none"}

# What a fresh install starts at; after that the composer saves the user's pick
# into effort_level (src/stores/settingsStore.ts setEffortLevel).
DEFAULT_EFFORT = "medium"

_OPUS_RE = re.compile(r"opus(\d+)\.(\d+)$", re.IGNORECASE)
_SONNET_RE = re.compile(r"sonnet(\d+)\.(\d+)$", re.IGNORECASE)
# The Bedrock id's own version, for a key that doesn't carry one. Opus and
# Sonnet ids spell the minor separately ("claude-opus-4-8",
# "claude-sonnet-5-5"), so capture both parts.
_OPUS_ID_VER_RE = re.compile(r"claude-opus-(\d+)(?:-(\d+))?", re.IGNORECASE)
_SONNET_ID_VER_RE = re.compile(r"claude-sonnet-(\d+)(?:-(\d+))?", re.IGNORECASE)
# The first Sonnet this app offers the xhigh rung on; the app keeps Sonnet 5
# on the standard ladder.
_SONNET_FULL_FROM = (5, 5)
# Haiku ids carry the version too ("claude-haiku-5-5",
# "claude-haiku-4-5-20251001-v1:0"). Haiku 5.5 is the first Haiku with
# adaptive thinking and effort (low to max, probed live 2026-11-16); earlier
# Haiku rejects output_config.effort.
_HAIKU_ID_VER_RE = re.compile(r"claude-haiku-(\d+)-(\d+)", re.IGNORECASE)
_HAIKU_EFFORT_FROM = (5, 5)


def haiku_has_effort(model_id: str) -> bool:
    """True for a Haiku id from 5.5 on: it thinks adaptively, takes effort,
    has a 128K output ceiling, and thinks by default when a request says
    nothing about thinking."""
    m = _HAIKU_ID_VER_RE.search(model_id or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= _HAIKU_EFFORT_FROM


def quiet_call_fields(model_id: str) -> dict:
    """Body fields that keep a short internal call (classifier, caption,
    title, OCR) from thinking.

    Haiku 5.5 thinks by default. On a call with a small max_tokens that
    thinking is billed against the limit and comes back as the first content
    block, so the reply is cut off or missing (measured 2026-11-16: a
    100-token classifier call stopped at max_tokens, a 10-token one returned
    no text). It accepts thinking "disabled", which restores the plain reply
    earlier Haiku gave. Other models get nothing: the ones that think by
    default reject "disabled"."""
    return {"thinking": {"type": "disabled"}} if haiku_has_effort(model_id) else {}


def infer_effort_tier(key: str, model_id: str = "") -> str:
    """Infer a model's raw reasoning ladder when the entry does not declare one.

    full:     Opus 4.8 and up, Sonnet 5.5 and up, Haiku 5.5 and up, and
              Fable (low to max, including the xhigh rung)
    standard: Sonnet up to 5 and Opus 4.0 to 4.7 (low/medium/high/max)
    none:     Haiku up to 4.5 (no effort/thinking)

    Reads the config key first, then falls back to the Bedrock model id, so a
    renamed or dotless entry ("my-opus", "opus5") keeps the ladder of the
    model it actually points at instead of silently degrading to standard and
    losing its top rung.
    """
    k = key.lower()
    if "haiku" in k:
        # The key carries no version ("haiku"), so the id decides.
        return "full" if haiku_has_effort(model_id) else "none"
    if "fable" in k:
        return "full"
    m = _OPUS_RE.match(k)
    if m and (int(m.group(1)), int(m.group(2))) >= (4, 8):
        return "full"
    m = _SONNET_RE.match(k)
    if m and (int(m.group(1)), int(m.group(2))) >= _SONNET_FULL_FROM:
        return "full"

    mid = (model_id or "").lower()
    if mid:
        if "haiku" in mid:
            return "full" if haiku_has_effort(mid) else "none"
        if "fable" in mid:
            return "full"
        m = _OPUS_ID_VER_RE.search(mid)
        if m and (int(m.group(1)), int(m.group(2) or 0)) >= (4, 8):
            return "full"
        m = _SONNET_ID_VER_RE.search(mid)
        if m and (int(m.group(1)), int(m.group(2) or 0)) >= _SONNET_FULL_FROM:
            return "full"
    return "standard"


def effort_tier_for(meta: dict | None, key: str) -> str:
    """This model's raw reasoning ladder name. Explicit config wins, then the
    key, then the Bedrock id (see infer_effort_tier)."""
    meta = meta or {}
    return meta.get("effort_tier") or infer_effort_tier(key, str(meta.get("id") or ""))


def infer_supports_ultracode(meta: dict | None, key: str) -> bool:
    """Whether a model can drive workflow orchestration, when its config entry
    does not say: every model on an orchestration-capable ladder."""
    return effort_tier_for(meta, key) in _ULTRACODE_TIERS


def supports_ultracode(meta: dict | None, key: str) -> bool:
    """Whether this model offers the ultracode level. An explicit
    ``supports_ultracode`` in the model metadata wins; otherwise inferred."""
    declared = (meta or {}).get("supports_ultracode")
    if declared is not None:
        return bool(declared)
    return infer_supports_ultracode(meta, key)


def effort_levels_for(meta: dict | None, key: str) -> list[str]:
    """Allowed effort labels for a model: its raw ladder, plus ``ultracode``
    when the model can orchestrate. An on-device model's ladder is its thinking
    levels, read from its own chat template (server/local/thinking.py)."""
    if (meta or {}).get("is_local"):
        from server.local.thinking import levels_for

        return levels_for(key)
    tier = effort_tier_for(meta, key)
    levels = list(EFFORT_TIERS.get(tier, EFFORT_TIERS["standard"]))
    if levels and supports_ultracode(meta, key):
        levels.append("ultracode")
    return levels


def clamp_effort(level: str, allowed: list[str]) -> str | None:
    """Clamp ``level`` to the nearest allowed label at or below it (matching
    Claude Code's own fallback rule). Returns ``None`` when the model has no
    effort support (empty ``allowed``). A level that thinks never clamps to
    ``none``: on a ladder of only ``none`` and ``max`` (an MLX model with no
    thinking cap), ``high`` keeps thinking at ``max``."""
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
        if lv == "none":
            continue
        i = EFFORT_ORDER.index(lv)
        if i <= ci and (best is None or i > EFFORT_ORDER.index(best)):
            best = lv
    if best:
        return best
    thinking = [lv for lv in allowed if lv != "none"]
    return thinking[0] if thinking else allowed[0]


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
    the model supports). Falls back to DEFAULT_EFFORT for effort-less models.
    An on-device model that can think starts at its own default level,
    clamped to its ladder (an MLX model with only off and max starts at max)."""
    allowed = effort_levels_for(meta, key)
    if (meta or {}).get("is_local") and allowed:
        from server.local.thinking import DEFAULT_LEVEL

        return clamp_effort(DEFAULT_LEVEL, allowed) or DEFAULT_LEVEL
    return clamp_effort(DEFAULT_EFFORT, allowed) or DEFAULT_EFFORT


def api_effort_for(level: str, meta: dict | None, key: str) -> str:
    """Map a friendly label to this model's Bedrock ``output_config.effort``.

    ``ultracode`` is an orchestration mode, not a wire value, so it rides the
    deepest reasoning rung the model actually has: ``xhigh`` where the ladder
    includes it, otherwise the ladder's own top. Sending ``xhigh`` to a model
    without that rung is exactly the error this avoids.
    """
    if level != "ultracode":
        return _LABEL_TO_API.get(level, "high")
    # Fall back to the SAME ladder effort_levels_for uses for an unrecognised
    # tier. Falling back to an empty ladder here instead sent the model-unaware
    # xhigh to a model whose config offered ultracode off the standard ladder —
    # a typo in effort_tier was enough to put a rung on the wire that the model
    # rejects.
    tier = EFFORT_TIERS.get(effort_tier_for(meta, key), EFFORT_TIERS["standard"])
    ladder = [lv for lv in tier if lv != "none"]
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
    """Map a raw wire alias (``'xhigh'``) onto its app label and any unknown
    value onto DEFAULT_EFFORT, so downstream code never sees a provider-specific
    or unknown value."""
    if level in EFFORT_ORDER:
        return level
    return _LABEL_ALIASES.get(str(level or "").lower(), DEFAULT_EFFORT)
