"""Configuration management with 3-layer merge and session-latching support.

Config layers (lowest to highest priority):
  1. DEFAULTS — built-in fallbacks
  2. User config — config.json (global user preferences)
  3. Project config — .whisper/settings.json in workspace root (per-project)

The latching system caches config snapshots per session so that mid-session
settings changes don't disrupt an active conversation or invalidate prompt caches.

Latching fields: bedrock_region, chat_models, default_chat_model, effort_level,
brief_mode, permission_mode. These are frozen at session start and
only refresh when a new session begins.
"""

import json
import logging
import os
import re
import threading
import time

from fastapi import APIRouter, Request

from server.infrastructure.effort import infer_effort_tier, normalize_effort

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/config", tags=["config"])

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json"
)
EXAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "config.example.json")

# The chat-model catalog has ONE source of truth: config.example.json (the
# template setup.sh copies to the user's config.json). The code keeps no second
# copy of it; we read the template's catalog here purely as the fallback for a
# config that defines no chat_models. Cached after the first read.
_seed_models_cache: dict | None = None


def _seed_chat_models() -> dict:
    """The chat-model catalog from config.example.json (the canonical template),
    used as the fallback when a config defines none. Returns {} (degraded, not a
    crash) if the template is missing or unreadable."""
    global _seed_models_cache
    if _seed_models_cache is None:
        try:
            with open(EXAMPLE_CONFIG_PATH) as f:
                _seed_models_cache = json.load(f).get("chat_models") or {}
        except Exception:
            _seed_models_cache = {}
    return _seed_models_cache


DEFAULTS = {
    # Where app data (result cache, JSON stores, DBs) lives. Empty = the
    # default <repo>/data. Overridable so a packaged/worktree install can pin
    # a stable location; see server/infrastructure/paths.py.
    "data_dir": "",
    "tavily_api_key": "",
    "whisper_language": None,
    # Which ASR backend the live recorder uses:
    #   "streaming" — alias for the Parakeet backend, word-by-word interims (default)
    #   "whisper"   — utterance-based mlx-whisper (proven fallback path)
    # Resolved via the registry in server/asr. Read once at /ws connect so
    # a mid-recording change doesn't swap models on a live session.
    "transcription_backend": "streaming",
    "bedrock_region": "us-east-1",
    # Chat-model catalog: the single source of truth is config.example.json
    # (copied to config.json by setup.sh). Loaded here ONLY as the fallback for a
    # config that defines no chat_models; a config.json's chat_models REPLACES
    # this wholesale (see load_config), so the code keeps no divergent copy and
    # there's nothing to drift against (this is what fixed the us.* vs global.*
    # mismatch and the duplicate Opus 4.6). To add/change a model, edit
    # config.example.json. Rich shape {key:{id,label,thinking,...}}; legacy flat
    # {key:"id"} is still accepted (see _normalize_chat_models).
    "chat_models": _seed_chat_models(),
    "default_chat_model": "opus4.8",
    "brief_mode": False,
    "permission_mode": "default",  # default | auto | plan | acceptEdits | bypassPermissions | dontAsk
    "permission_explainer_enabled": True,
    # Local mode (the `local` branch's on-device build). When on: transcription
    # models load into memory lazily on first use and unload when the user
    # switches engines (one resident at a time), and on-device LLMs are
    # available. Default False so main behaves exactly as before.
    "local_mode": False,
    # Where indexing/RAG capabilities run. "cloud" (default) = all Amazon
    # Bedrock; "local" = all on-device; "hybrid" = per-capability via "backends"
    # below. Resolved in server/infrastructure/model_mode.py. Also drives which
    # chat models the picker shows (cloud hides on-device models, and vice versa).
    "model_mode": "cloud",
    # Per-capability backend overrides, consulted ONLY in "hybrid" mode.
    # Keys: embed (cohere|qwen3), rerank (cohere|qwen3), ner (haiku|gliner),
    # index_llm (haiku|local|none). Unset capabilities fall back to the cloud
    # backend.
    "backends": {},
    "effort_level": "high",
    "auto_mode_enabled": False,
    "auto_mode_allow": [],
    "auto_mode_soft_deny": [],
    "auto_mode_environment": [],
    # Cost management
    "max_session_cost_usd": 0.0,  # 0 = unlimited
    "max_daily_cost_usd": 0.0,  # 0 = unlimited
    "model_fallback_enabled": False,  # Enable opus→sonnet→haiku fallback chain
    # Scheduled tasks (cron). Timezone: "" means use the host system timezone
    # (resolved from /etc/localtime in server/cron_scheduler.py). A per-job
    # schedule.tz always overrides this, so wall-clock jobs ("daily at 09:00")
    # fire at the intended local hour and stay correct across DST.
    "cron_timezone": "",
    # Per-job run-history retention in the cron_runs table.
    "cron_max_runs_per_job": 200,
    # How late a missed fire may still run after the server was down (seconds).
    # Booted 09:15 for a 09:00 job → still runs; booted 11:00 → skips the day.
    "cron_misfire_grace_sec": 3600,
    # Feature flags namespace — see server/feature_flags.py
    "feature_flags": {},
}

# Fields that are latched (frozen) per session to prevent mid-session
# prompt cache invalidation or behavioral drift.
LATCHED_FIELDS = frozenset(
    {
        "bedrock_region",
        "chat_models",
        "chat_model_meta",
        "default_chat_model",
        "effort_level",
        "brief_mode",
        "permission_mode",
    }
)

# Keys allowed in project-level .whisper/settings.json
# (subset of DEFAULTS — exclude global-only keys like API keys)
PROJECT_SETTINGS_KEYS = frozenset(
    {
        "bedrock_region",
        "chat_models",
        "default_chat_model",
        "effort_level",
        "brief_mode",
        "permission_mode",
        "permission_explainer_enabled",
        "auto_mode_enabled",
        "auto_mode_allow",
        "auto_mode_soft_deny",
        "auto_mode_environment",
        "max_session_cost_usd",
        "max_daily_cost_usd",
        "model_fallback_enabled",
        "feature_flags",
    }
)

# ── Global config cache ──────────────────────────────────────────────
_config_cache: dict | None = None
_config_cache_mtime: float = 0.0
_config_cache_lock = threading.Lock()
_CONFIG_CACHE_TTL = 2.0  # seconds


_OPUS_VERSION_RE = re.compile(r"opus(\d+)\.(\d+)$", re.IGNORECASE)


def _infer_thinking_default(key: str) -> str:
    """Pick the thinking mode for a legacy flat-string entry that doesn't
    declare one. Opus 4.7+ supports adaptive thinking (no fixed budget);
    everything else uses an explicit budget. Pattern-matched so a future
    Opus 4.9 / 5.0 in old-shape config.json also resolves correctly."""
    m = _OPUS_VERSION_RE.match(key)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        if (major, minor) >= (4, 7):
            return "adaptive"
    return "budget"


def _normalize_chat_models(chat_models: dict) -> tuple[dict, dict]:
    """Split a chat_models map into (ids, meta).

    Accepts BOTH shapes side-by-side:
      - Legacy flat:  {"opus4.6": "global.anthropic.claude-opus-4-6-v1"}
      - Rich:         {"opus4.6": {"id": "...", "label": "Opus 4.6", "thinking": "budget"}}

    Returns:
        ids:  {key: bedrock_model_id}      — what every existing callsite uses
        meta: {key: {label, thinking}}     — new, for /api/models and chat.py
    """
    ids: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for key, val in chat_models.items():
        if isinstance(val, str):
            ids[key] = val
            meta[key] = {
                "label": key.capitalize(),
                "thinking": _infer_thinking_default(key),
                "requires_data_retention": False,
                "effort_tier": infer_effort_tier(key),
            }
        elif isinstance(val, dict) and isinstance(val.get("id"), str):
            model_id = val["id"]
            ids[key] = model_id
            # Inference provider. "anthropic" (default) goes through the
            # bedrock-runtime Converse path; "openai_bedrock" routes to the
            # OpenAI Responses API on bedrock-mantle (server/openai_bedrock).
            # Explicit wins; otherwise inferred from the model id so an OpenAI
            # model added to config is as turnkey as a Claude one — just give it
            # an ``openai.`` id and it routes correctly.
            provider = val.get("provider") or (
                "openai_bedrock" if model_id.startswith("openai.") else "anthropic"
            )
            meta[key] = {
                "label": val.get("label") or key.capitalize(),
                "thinking": val.get("thinking") or _infer_thinking_default(key),
                # Mythos-class models (e.g. Fable 5) require the AWS account's
                # Bedrock data-retention mode to be provider_data_share. The UI
                # uses this flag to gate model selection behind a consent screen.
                "requires_data_retention": bool(val.get("requires_data_retention", False)),
                # On-device model (local branch) — runs via the local runtime,
                # not Bedrock. Surfaced on /api/models for the picker badge.
                "is_local": bool(val.get("is_local", False)),
                # Whether this local model has a toggleable thinking mode.
                "supports_thinking": bool(val.get("supports_thinking", False)),
                # Whether this local model can use tools (local agentic loop).
                "supports_tools": bool(val.get("supports_tools", False)),
                # Which effort levels this model exposes (full/standard/none/openai).
                # Explicit wins; OpenAI models default to the "openai" ladder,
                # everything else infers from the key. Drives the per-model effort
                # picker and the Bedrock output_config.effort.
                "effort_tier": val.get("effort_tier")
                or ("openai" if provider == "openai_bedrock" else infer_effort_tier(key)),
                "provider": provider,
                # OpenAI-on-Bedrock only: an optional per-model region override
                # (absent ⇒ the account-wide bedrock_region is used, same as the
                # Anthropic path) and the GPT-5.x verbosity (text.verbosity).
                # Ignored by other providers.
                "openai_region": val.get("openai_region"),
                "verbosity": val.get("verbosity") or "medium",
            }
        # Silently skip malformed entries — a typo in config.json shouldn't
        # take the server down. The model just won't appear in the picker.
    return ids, meta


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay into base. Nested dicts are merged recursively; lists replace."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_user_config() -> dict:
    """Load user-level config.json from disk."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_project_config(workspace_path: str | None = None) -> dict:
    """Load project-level .whisper/settings.json from workspace root."""
    if not workspace_path:
        return {}
    settings_path = os.path.join(workspace_path, ".whisper", "settings.json")
    try:
        with open(settings_path) as f:
            stored = json.load(f)
        # Only allow project-safe keys
        return {k: v for k, v in stored.items() if k in PROJECT_SETTINGS_KEYS}
    except Exception:
        return {}


def _env_overlay() -> dict:
    """Read sensitive secrets from environment variables. These override
    anything stored in config.json so users can keep keys out of disk
    (e.g. export TAVILY_API_KEY=... in their shell rc, rotate via env
    without re-editing config). Empty env values are ignored — config
    falls back to whatever's in DEFAULTS or config.json.
    """
    overlay: dict = {}
    tavily = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily:
        overlay["tavily_api_key"] = tavily
    return overlay


def load_config(workspace_path: str | None = None) -> dict:
    """Load merged config: DEFAULTS → user config → project config → env.

    Uses a short TTL cache for the user layer. Project layer is always
    read fresh (it changes when workspace changes). Env-var overlay is
    re-read on every load so a rotated key takes effect without a
    restart.
    """
    global _config_cache, _config_cache_mtime

    now = time.monotonic()

    # Layer 1+2: DEFAULTS + user config (cached)
    with _config_cache_lock:
        if _config_cache is not None and (now - _config_cache_mtime) < _CONFIG_CACHE_TTL:
            user_merged = _config_cache
        else:
            user_merged = None

    if user_merged is None:
        user_stored = _load_user_config()
        user_merged = _deep_merge(DEFAULTS, user_stored)
        # chat_models is CONFIG-AUTHORITATIVE: when config.json provides a model
        # catalog it REPLACES the built-in DEFAULTS wholesale, rather than being
        # unioned key-by-key underneath it. The config file is the single source
        # of truth for the model list, so a renamed/removed model can't collide
        # with (or be resurrected by) a hardcoded default — that key-drift union
        # is exactly what used to surface "Opus 4.6" twice. DEFAULTS' catalog is
        # only the fallback when config defines no chat_models (e.g. empty/first-
        # run config).
        if isinstance(user_stored.get("chat_models"), dict) and user_stored["chat_models"]:
            user_merged["chat_models"] = user_stored["chat_models"]
        # Backward compat: local_mode predates model_mode. If the user runs an
        # on-device config (local_mode on) but never set model_mode, follow
        # local_mode so their on-device models stay visible/usable instead of
        # being hidden by the cloud default. An explicit model_mode always wins.
        if "model_mode" not in user_stored and user_merged.get("local_mode"):
            user_merged["model_mode"] = "local"
        with _config_cache_lock:
            _config_cache = user_merged
            _config_cache_mtime = now

    # Layer 3: project config (not cached — workspace can change)
    project_stored = _load_project_config(workspace_path)
    merged = _deep_merge(user_merged, project_stored) if project_stored else dict(user_merged)
    # Project config is config-authoritative for the catalog too: a project that
    # lists chat_models replaces, it doesn't union onto the user/DEFAULTS catalog.
    if isinstance(project_stored.get("chat_models"), dict) and project_stored["chat_models"]:
        merged["chat_models"] = project_stored["chat_models"]

    # Layer 4: environment-variable overlay (highest priority).
    # Re-read every call — cheap and lets a rotated key apply
    # immediately without invalidating the user-config cache.
    env = _env_overlay()
    if env:
        merged.update(env)

    # Normalize chat_models AFTER all layers have merged. This is what makes
    # the config single-source-of-truth: every callsite reads the same flat
    # {key: id} map, while /api/models and chat.py read meta from the parallel
    # chat_model_meta key. The on-disk rich shape is never re-flattened back
    # to config.json — see update_config below for the save path.
    if "chat_models" in merged:
        ids, meta = _normalize_chat_models(merged["chat_models"])
        merged["chat_models"] = ids
        merged["chat_model_meta"] = meta

    # Coerce legacy/unknown effort values (e.g. the retired "auto") to a known
    # label so downstream code and the per-model clamp never see a stale value.
    if "effort_level" in merged:
        merged["effort_level"] = normalize_effort(merged.get("effort_level"))

    # bedrock_region must never be empty. An empty/blank string defeats both the
    # _deep_merge default above (a present "" overwrites DEFAULTS) and dict.get's
    # default (only fires when the key is ABSENT), so it flows into
    # boto3.client(region_name="") and builds the malformed endpoint
    # "https://bedrock-runtime..amazonaws.com", a ValueError that 500s every
    # Bedrock callsite. Coerce blank/missing back to the default and strip stray
    # whitespace so no callsite can construct a client with region="".
    region = merged.get("bedrock_region")
    merged["bedrock_region"] = (
        region.strip() if isinstance(region, str) and region.strip() else DEFAULTS["bedrock_region"]
    )

    # default_chat_model has the same present-but-empty hazard: a blank string
    # defeats dict.get's default and then misses lookup in chat_models, leaving
    # model resolution to limp on the "sonnet"/first fallbacks with the wrong
    # cost + thinking metadata. Coerce blank/missing to the default; and if the
    # configured key isn't a known model, fall back to the default (or the first
    # available model) so _get_default_model() always returns a resolvable key.
    chat_model_keys = merged.get("chat_models") or {}
    default_model = merged.get("default_chat_model")
    if not isinstance(default_model, str) or not default_model.strip():
        default_model = DEFAULTS["default_chat_model"]
    else:
        default_model = default_model.strip()
    if chat_model_keys and default_model not in chat_model_keys:
        default_model = (
            DEFAULTS["default_chat_model"]
            if DEFAULTS["default_chat_model"] in chat_model_keys
            else next(iter(chat_model_keys))
        )
    merged["default_chat_model"] = default_model

    return merged


def _invalidate_cache():
    """Force the next load_config() to re-read from disk."""
    global _config_cache
    with _config_cache_lock:
        _config_cache = None


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    _invalidate_cache()


def _write_config_text(text: str) -> None:
    with open(CONFIG_PATH, "w") as f:
        f.write(text)
    _invalidate_cache()


def _json_object_span(text: str, key: str) -> tuple[int, int] | None:
    """Span (start, end) of the ``{...}`` value of a top-of-mind `key`, where
    start indexes the opening brace and end is one past the matching close.
    Brace-matched and quote-aware so it ignores braces inside strings."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not m:
        return None
    i = m.end() - 1  # the '{'
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (i, j + 1)
    return None


def set_feature_flag(flag_name: str, enabled: bool) -> None:
    """Toggle ONE feature flag in config.json, changing only that boolean and
    preserving every other byte and all formatting.

    A flag toggle should be surgical. Re-serializing the whole file via
    save_config()/json.dump reflows formatting (e.g. collapses aligned
    chat_models one-liners) and historically flattened the rich chat_models
    shape, silently dropping fields like requires_data_retention. This patches
    the flag's value in place inside the feature_flags object; it only falls
    back to a structured write when config.json has no feature_flags block at
    all (rare/first-run)."""
    literal = "true" if enabled else "false"
    try:
        with open(CONFIG_PATH) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""

    span = _json_object_span(text, "feature_flags") if text.strip() else None
    if span is not None:
        start, end = span
        segment = text[start:end]
        pattern = re.compile(r'("' + re.escape(flag_name) + r'"\s*:\s*)(?:true|false)\b')
        new_segment, n = pattern.subn(lambda mm: mm.group(1) + literal, segment)
        if n >= 1:
            _write_config_text(text[:start] + new_segment + text[end:])
            return
        # Flag not present yet — insert it, matching the block's indentation.
        if segment[1:-1].strip() == "":
            new_segment = f'{{\n    "{flag_name}": {literal}\n  }}'
        else:
            ind = re.search(r'\n(\s*)"', segment)
            indent = ind.group(1) if ind else "    "
            new_segment = "{\n" + indent + f'"{flag_name}": {literal},' + segment[1:]
        _write_config_text(text[:start] + new_segment + text[end:])
        return

    # No feature_flags object on disk — structured write of the raw config.
    raw = _load_user_config()
    flags = raw.get("feature_flags") or {}
    flags[flag_name] = enabled
    raw["feature_flags"] = flags
    save_config(raw)


def get(key: str, default=None):
    """Get a single config value (user-level only, no workspace context)."""
    return load_config().get(key, default)


# ── Session latching ─────────────────────────────────────────────────

# {session_id: {field: value, ...}}
_latched_sessions: dict[str, dict] = {}
_latched_lock = threading.Lock()


def latch_session(session_id: str, workspace_path: str | None = None) -> dict:
    """Snapshot latched config fields for a session.

    Called once at the start of a chat request. If the session already has
    a snapshot, returns it unchanged (latched). Otherwise creates one from
    the current config (including project-level overrides).

    Returns the full config with latched fields applied.
    """
    config = load_config(workspace_path)

    with _latched_lock:
        if session_id not in _latched_sessions:
            snapshot = {k: config[k] for k in LATCHED_FIELDS if k in config}
            _latched_sessions[session_id] = snapshot
            log.debug("Latched config for session %s: %s", session_id, list(snapshot.keys()))

        # Overlay latched values onto fresh config
        latched = _latched_sessions[session_id]

    result = dict(config)
    result.update(latched)
    return result


def unlatch_session(session_id: str):
    """Release the latched snapshot for a session.

    Call when a session ends or the user explicitly requests a config refresh.
    """
    with _latched_lock:
        removed = _latched_sessions.pop(session_id, None)
    if removed:
        log.debug("Unlatched config for session %s", session_id)


# ── API ──────────────────────────────────────────────────────────────


@router.get("")
async def get_config_endpoint():
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    config = load_config(ws)
    safe = dict(config)
    if safe.get("tavily_api_key"):
        key = safe["tavily_api_key"]
        safe["tavily_api_key_masked"] = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
    else:
        safe["tavily_api_key_masked"] = ""
    # Never expose the raw secret over the API — the UI only needs the masked
    # hint, and only sends a new key when the user types one.
    safe.pop("tavily_api_key", None)
    # Include feature flag states
    try:
        from server.infrastructure.feature_flags import get_flag_states

        safe["_feature_flag_states"] = get_flag_states()
    except Exception:
        pass
    # Include config layer info
    safe["_has_project_config"] = bool(_load_project_config(ws))
    return safe


@router.put("")
async def update_config(request: Request):
    body = await request.json()
    # Use the RAW on-disk config as the base, not load_config(). load_config
    # flattens chat_models for downstream callers — if we wrote that back we'd
    # lose the rich {id, label, thinking} shape and the per-model metadata.
    # Body fields overlay on top; everything else is preserved verbatim.
    raw = _load_user_config()
    # These keys must never be persisted blank: an empty string defeats the
    # config default (dict.get only defaults an ABSENT key) and breaks Bedrock
    # region/model resolution downstream. Drop a blank value so the existing
    # (or DEFAULTS) value is preserved. The UI guards too, but this also covers
    # project settings and direct API calls.
    non_empty_keys = ("bedrock_region", "default_chat_model")
    for key in DEFAULTS:
        if key not in body:
            continue
        value = body[key]
        if key in non_empty_keys:
            if not isinstance(value, str) or not value.strip():
                log.warning("Ignoring empty %s in config update", key)
                continue
            value = value.strip()
        raw[key] = value
    save_config(raw)
    log.info("Config updated: %s", list(body.keys()))
    return {"updated": True}
