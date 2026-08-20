"""Configuration management with a layered merge and session-latching support.

Config layers (lowest to highest priority):
  1. DEFAULTS — built-in fallbacks (code)
  2. SYSTEM — the bundle's config.example.json at repo_root() (read-only; ships
     inside the .app and updates with every app version). The app-owned model
     catalog + shipped defaults.
  3. USER — config.user.json in the app home (the user owns it; survives app
     updates; the in-settings editor writes here). Holds only the user's deltas:
     added local models, region/keys/flags, per-field overrides, and a hide-list.
     Back-compat: when config.user.json is absent but a legacy config.json exists
     (a dev checkout, or a packaged install that hasn't migrated yet), the legacy
     config.json IS the user layer until migrate_user_config() runs.
  4. PROJECT — .whisper/settings.json in the workspace root (per-project)
  5. ENV — sensitive secrets from environment variables

``chat_models`` is an ADDITIVE per-key deep-merge across SYSTEM and USER: the
shipped catalog always appears, and a user entry adds a new key or overrides
specific fields of a shipped entry. ``chat_models_disabled`` (a list of keys in
the USER and PROJECT layers) drops shipped models the user doesn't want after
the merge. The PROJECT layer still replaces the catalog wholesale, matching its
historical semantics. Pricing stays SYSTEM-only (pricing.json) — there is no
user pricing layer, because user-added local models are always $0.

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
import shutil
import tempfile
import threading
import time

from fastapi import APIRouter, Request

from server.infrastructure.effort import (
    infer_effort_tier,
    infer_supports_ultracode,
    normalize_effort,
)
from server.infrastructure.paths import config_dir, repo_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/config", tags=["config"])

# The user's live config follows the app home (WHISPER_HOME in a packaged
# install, the repo root in a dev checkout); the committed template always
# stays with the code.
#
# USER_CONFIG_PATH (config.user.json) is the user layer the app writes to going
# forward. CONFIG_PATH (config.json) is the legacy monolith: it acts as the user
# layer only until migrate_user_config() splits it (see _active_user_config_path).
USER_CONFIG_PATH = os.path.join(config_dir(), "config.user.json")
CONFIG_PATH = os.path.join(config_dir(), "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(repo_root(), "config.example.json")

# The SYSTEM layer is the full config.example.json (the app-owned catalog +
# shipped defaults). It is read-only in the bundle, so we cache it keyed on the
# file's mtime — a rebuilt bundle (new mtime) is picked up, but steady-state
# loads never re-read from disk.
_system_cache: dict | None = None
_system_cache_key: tuple | None = None


def _system_config() -> dict:
    """The full SYSTEM layer: config.example.json parsed as a config dict.
    Returns {} (degraded, not a crash) if the template is missing or unreadable.
    Cached on (path, mtime) so tests that swap EXAMPLE_CONFIG_PATH still work."""
    global _system_cache, _system_cache_key
    path = EXAMPLE_CONFIG_PATH
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, None)
    if _system_cache is not None and _system_cache_key == key:
        return _system_cache
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _system_cache = data
    _system_cache_key = key
    return data


def _seed_chat_models() -> dict:
    """The chat-model catalog from the SYSTEM layer (config.example.json), used
    as the ultimate fallback for DEFAULTS when no layer defines chat_models.
    Returns {} (degraded, not a crash) if the template is missing/unreadable."""
    return _system_config().get("chat_models") or {}


DEFAULTS = {
    # Where app data (result cache, JSON stores, DBs) lives. Empty = the
    # default <repo>/data. Overridable so a packaged/worktree install can pin
    # a stable location; see server/infrastructure/paths.py.
    "data_dir": "",
    "tavily_api_key": "",
    # Whisper decode language: blank/None = auto-detect, "pl" pins one language,
    # "pl,en" keeps auto-detection but constrains it to that allowlist.
    "whisper_language": None,
    # Whisper engine only: also decode each non-English utterance a second
    # time with task=translate and show the English under the transcript.
    "whisper_translate": False,
    # Which ASR backend the live recorder uses:
    #   "streaming" — alias for the Parakeet backend, word-by-word interims (default)
    #   "whisper"   — utterance-based mlx-whisper (proven fallback path)
    # Resolved via the registry in server/asr. Read once at /ws connect so
    # a mid-recording change doesn't swap models on a live session.
    "transcription_backend": "streaming",
    "bedrock_region": "us-east-1",
    # Chat-model catalog. The app-owned catalog lives in config.example.json (the
    # SYSTEM layer); this DEFAULTS copy is sourced from it so the two can't drift,
    # and serves only as the ultimate fallback when no layer defines chat_models.
    # SYSTEM and USER deep-merge ADDITIVELY per key (see load_config): the shipped
    # catalog always appears and a USER entry adds a key or overrides fields of a
    # shipped one. To add/change a shipped model, edit config.example.json; users
    # add their own via config.user.json. Rich shape {key:{id,label,thinking,...}};
    # legacy flat {key:"id"} is still accepted (see _normalize_chat_models).
    "chat_models": _seed_chat_models(),
    # Model keys to HIDE from the effective catalog after the SYSTEM+USER merge.
    # Lets a user drop a shipped model they don't want without deleting it from
    # the app-owned catalog. Honored in the USER and PROJECT layers (unioned);
    # unknown names are ignored. Default [] = show system models + the user's own.
    "chat_models_disabled": [],
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
    # Keys: embed (cohere|qwen3), rerank (cohere|qwen3), ner (haiku, gliner, or
    # an on-device model key for LLM NER), index_llm (haiku, an on-device model
    # key, "local" to follow the active local model, or none). Unset
    # capabilities fall back to the cloud backend.
    "backends": {},
    "effort_level": "high",
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
        "chat_models_disabled",
        "default_chat_model",
        "effort_level",
        "brief_mode",
        "permission_mode",
        "permission_explainer_enabled",
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


def _as_int(v) -> int | None:
    """Coerce a config-supplied number to int, or None if absent/malformed."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


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
                "id": val,
                "label": key.capitalize(),
                "thinking": _infer_thinking_default(key),
                "requires_data_retention": False,
                "effort_tier": infer_effort_tier(key, val),
                "context_window": None,
                "max_output": None,
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
                # The Bedrock id, alongside the ids map. Capability inference
                # reads it so a renamed config key ("my-sonnet5") keeps the
                # capabilities of the model it actually points at.
                "id": model_id,
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
                or ("openai" if provider == "openai_bedrock" else infer_effort_tier(key, model_id)),
                # Whether this model can drive workflow orchestration. Kept
                # SEPARATE from effort_tier: the tier says which raw reasoning
                # values the provider accepts, this says whether the model can
                # author and run a workflow. Sonnet 5 orchestrates on a standard
                # ladder. None ⇒ inferred (see effort.supports_ultracode).
                "supports_ultracode": val.get("supports_ultracode"),
                "provider": provider,
                # OpenAI-on-Bedrock only: an optional per-model region override
                # (absent ⇒ the account-wide bedrock_region is used, same as the
                # Anthropic path) and the GPT-5.x verbosity (text.verbosity).
                # Ignored by other providers.
                "openai_region": val.get("openai_region"),
                "verbosity": val.get("verbosity") or "medium",
                # On-device weights, for is_local entries only. Carrying these on
                # the SAME entry is what makes a new local model a one-place
                # config change: the picker metadata above and the loader info
                # here live together, so dropping a model into chat_models is
                # enough for the registry to download and serve it. Absent for
                # cloud models (and for local entries that rely on a built-in
                # default). See server/local/registry.py.
                "repo_id": val.get("repo_id"),
                "filename": val.get("filename"),
                "dir": val.get("dir"),
                "ctx": val.get("ctx"),
                # Context accounting (turn engine): the model's total input
                # window and max output tokens. Absent ⇒ per-family defaults
                # (see server/chat/engine/windows.py); local models resolve
                # from the live n_ctx instead.
                "context_window": _as_int(val.get("context_window")),
                "max_output": _as_int(val.get("max_output")),
            }
        # Silently skip malformed entries — a typo in config.json shouldn't
        # take the server down. The model just won't appear in the picker.
        # When the entry does not declare orchestration capability (legacy
        # flat strings never do, rich entries may omit it), infer it from the
        # actual model id via effort.infer_supports_ultracode so raw
        # load_config() consumers match startup behavior. Explicit true/false
        # values pass through untouched.
        entry = meta.get(key)
        if entry is not None and entry.get("supports_ultracode") is None:
            entry["supports_ultracode"] = infer_supports_ultracode(entry, key)
    return ids, meta


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay into base. Nested dicts are merged recursively; lists replace.

    ``chat_models`` (a dict of per-model dicts) therefore deep-merges ADDITIVELY
    across layers for free: SYSTEM's shipped entries are preserved and a USER
    entry adds a key or overrides individual fields of a shipped one."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _active_user_config_path() -> str:
    """The file that IS the user layer right now.

    config.user.json once it exists (post-migration, or a packaged install whose
    boot created it); otherwise the legacy config.json when only that exists (a
    dev checkout, or a packaged install that hasn't migrated yet); otherwise
    config.user.json — the canonical target for a first write on a clean home.
    Reading the module attributes each call keeps it monkeypatch-friendly."""
    if os.path.exists(USER_CONFIG_PATH):
        return USER_CONFIG_PATH
    if os.path.exists(CONFIG_PATH):
        return CONFIG_PATH
    return USER_CONFIG_PATH


def _load_user_config() -> dict:
    """Load the USER layer from disk (config.user.json, or the legacy config.json
    while that is still the active user layer)."""
    try:
        with open(_active_user_config_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _disabled_keys(layer: dict) -> set[str]:
    """The valid string entries of a layer's ``chat_models_disabled`` list.
    Non-list values and non-string entries are ignored (validation lives in the
    raw editor); this is the tolerant runtime read."""
    lst = layer.get("chat_models_disabled")
    if not isinstance(lst, list):
        return set()
    return {x for x in lst if isinstance(x, str)}


def _apply_disabled(cfg: dict, disabled: set[str]) -> None:
    """Drop hidden model keys from ``cfg['chat_models']``, replacing the dict with
    a filtered copy (never mutating the possibly-shared/cached original)."""
    if not disabled:
        return
    cm = cfg.get("chat_models")
    if isinstance(cm, dict):
        cfg["chat_models"] = {k: v for k, v in cm.items() if k not in disabled}


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
    """Load merged config: DEFAULTS → SYSTEM → USER → project → env.

    Uses a short TTL cache for the DEFAULTS+SYSTEM+USER result. Project layer is
    always read fresh (it changes when workspace changes). Env-var overlay is
    re-read on every load so a rotated key takes effect without a restart.
    """
    global _config_cache, _config_cache_mtime

    now = time.monotonic()

    # Layers 1-3: DEFAULTS + SYSTEM (config.example.json) + USER (config.user.json
    # or the legacy config.json). Cached together.
    with _config_cache_lock:
        if _config_cache is not None and (now - _config_cache_mtime) < _CONFIG_CACHE_TTL:
            user_merged = _config_cache
        else:
            user_merged = None

    if user_merged is None:
        system_stored = _system_config()
        user_stored = _load_user_config()
        # DEFAULTS → SYSTEM → USER. Because _deep_merge recurses into nested
        # dicts, chat_models merges ADDITIVELY per key/field with no special
        # case: SYSTEM's shipped catalog stays, and a USER entry adds a key or
        # overrides individual fields of a shipped one. (DEFAULTS' catalog is
        # sourced from SYSTEM, so merging it first is a harmless no-op and only
        # matters as the fallback when SYSTEM has none.)
        user_merged = _deep_merge(DEFAULTS, system_stored)
        user_merged = _deep_merge(user_merged, user_stored)
        # Hide-list: drop the models the USER asked to hide from the merged
        # catalog. (Project adds its own below.)
        _apply_disabled(user_merged, _disabled_keys(user_stored))
        # Backward compat: local_mode predates model_mode. If the user runs an
        # on-device config (local_mode on) but never set model_mode, follow
        # local_mode so their on-device models stay visible/usable instead of
        # being hidden by the cloud default. Keyed on the USER layer (SYSTEM
        # always ships model_mode, so the merged value is never "absent");
        # an explicit user model_mode always wins.
        if "model_mode" not in user_stored and user_merged.get("local_mode"):
            user_merged["model_mode"] = "local"
        with _config_cache_lock:
            _config_cache = user_merged
            _config_cache_mtime = now

    # Layer 4: project config (not cached — workspace can change)
    project_stored = _load_project_config(workspace_path)
    merged = _deep_merge(user_merged, project_stored) if project_stored else dict(user_merged)
    # Project config keeps its historical wholesale-replace semantics for the
    # catalog: a project that lists chat_models replaces the merged SYSTEM+USER
    # catalog rather than unioning onto it.
    if isinstance(project_stored.get("chat_models"), dict) and project_stored["chat_models"]:
        merged["chat_models"] = project_stored["chat_models"]
    # Apply the project's hide-list on top of the (possibly replaced) catalog.
    # _apply_disabled replaces the dict reference, so the cached user_merged
    # catalog is never mutated.
    _apply_disabled(merged, _disabled_keys(project_stored))

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


def _atomic_write(path: str, text: str) -> None:
    """Write ``text`` to ``path`` ATOMICALLY (temp file + rename in the same
    directory), so a crash mid-write can never leave a truncated file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(config: dict):
    """Persist the USER layer (config.user.json, or the legacy config.json while
    that is still the active user layer)."""
    _atomic_write(_active_user_config_path(), json.dumps(config, indent=2))
    _invalidate_cache()


def _write_config_text(text: str) -> None:
    """Persist raw USER-layer text ATOMICALLY, then drop the in-memory cache so
    the next load_config() sees the new contents. Targets the active user layer
    file (config.user.json going forward, or the legacy config.json until the
    split runs)."""
    _atomic_write(_active_user_config_path(), text)
    _invalidate_cache()


# Written into a brand-new config.user.json on first launch (packaged app, or
# any fresh home). After the file exists it is never overwritten, so the user's
# saved settings win from the second launch on. Defaults: hybrid mode with the
# index/RAG capabilities on-device where the weights download on demand —
# on-device embeddings + reranker (qwen3) and GLiNER entity extraction. The
# one-shot writer / relationship mapping (``index_llm``) defaults to cloud Haiku
# because the app ships NO local chat model: a fresh install has none on disk, so
# defaulting index_llm to a local key would point at a missing model. Once the
# user installs a local chat model from Discover, the install flips index_llm to
# it (server/model_browser/service.py). Chat model choice is the system default.
FIRST_RUN_USER_CONFIG = {
    "model_mode": "hybrid",
    "backends": {
        "embed": "qwen3",
        "rerank": "qwen3",
        "ner": "gliner",
        "index_llm": "haiku",
    },
}


def migrate_user_config() -> bool:
    """One-time, idempotent split of a monolithic config.json into a
    config.user.json that holds only the user's deltas.

    - If config.user.json already exists: no-op (already migrated / user layer
      present). Never overwritten.
    - If only a legacy config.json exists: back it up to config.json.bak.<ts>,
      write config.user.json with the user's deltas (chat_models entries that are
      genuinely new or that differ from the SYSTEM catalog, plus ALL non-catalog
      keys verbatim, including chat_models_disabled), then rename the legacy file
      to config.json.pre-split so it can't be double-read as a stale layer.
    - If neither exists (fresh install): create config.user.json as {} — the
      shipped catalog comes from the SYSTEM layer, so nothing is copied down.

    Returns True only when it performed an actual legacy→user migration.
    """
    # Resolve the home at CALL time (not the import-frozen module constants) so a
    # packaged boot always migrates within the live WHISPER_HOME even if the
    # module was imported before the env was set.
    home = config_dir()
    user_path = os.path.join(home, "config.user.json")
    legacy_path = os.path.join(home, "config.json")

    if os.path.exists(user_path):
        return False

    if not os.path.exists(legacy_path):
        # Fresh install: seed the first-run defaults (hybrid + on-device index),
        # then never touch the file again so the user's settings win afterward.
        # SYSTEM still supplies the chat-model catalog.
        try:
            _atomic_write(user_path, json.dumps(FIRST_RUN_USER_CONFIG, indent=2) + "\n")
            _invalidate_cache()
        except OSError as e:
            log.warning("Could not create %s: %s", user_path, e)
        return False

    try:
        with open(legacy_path) as f:
            legacy = json.load(f)
    except Exception as e:
        log.warning("Could not read legacy config.json for migration: %s", e)
        return False
    if not isinstance(legacy, dict):
        legacy = {}

    # Timestamped backup (setup.sh's config.json.bak.<YYYYmmddHHMMSS> convention).
    backup = f"{legacy_path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
    try:
        shutil.copyfile(legacy_path, backup)
        log.info("Backed up %s to %s before the config split.", legacy_path, backup)
    except OSError as e:
        log.warning("Could not back up %s before migration: %s", legacy_path, e)

    system_catalog = _system_config().get("chat_models")
    if not isinstance(system_catalog, dict):
        system_catalog = {}

    user_cfg: dict = {}
    for key, value in legacy.items():
        if key == "chat_models":
            continue
        # Every non-catalog key is user-owned — carried over verbatim (region,
        # api keys, model_mode, flags, data_dir, chat_models_disabled, …).
        user_cfg[key] = value

    legacy_cm = legacy.get("chat_models")
    if isinstance(legacy_cm, dict):
        # Keep only genuine additions (key absent from SYSTEM) and real overrides
        # (value differs deeply from the SYSTEM entry). Redundant copies of a
        # shipped entry are dropped so future SYSTEM updates flow through.
        delta = {
            k: v for k, v in legacy_cm.items() if k not in system_catalog or v != system_catalog[k]
        }
        if delta:
            user_cfg["chat_models"] = delta

    try:
        _atomic_write(user_path, json.dumps(user_cfg, indent=2) + "\n")
    except OSError as e:
        log.warning("Could not write %s during migration: %s", user_path, e)
        return False

    # Rename the legacy file out of the way so it is not read as a stale layer.
    # The .bak already preserves the original contents.
    try:
        os.replace(legacy_path, f"{legacy_path}.pre-split")
    except OSError as e:
        log.warning("Could not rename %s after migration: %s", legacy_path, e)

    _invalidate_cache()
    log.info(
        "Migrated config.json -> config.user.json (%d top-level key(s), %d chat_models delta).",
        len(user_cfg),
        len(user_cfg.get("chat_models", {})),
    )
    return True


def unfiltered_chat_models() -> tuple[dict, dict]:
    """The normalized ``(ids, meta)`` of the DEFAULTS→SYSTEM→USER chat catalog
    WITHOUT the ``chat_models_disabled`` hide-list applied.

    Weight-management surfaces read this instead of load_config(): the local
    registry (and through it the Settings > Models download manager) must keep
    serving a disabled model so its weights stay manageable — disabling only
    hides a model from the composer picker, it never strands gigabytes on disk.
    The picker path (/api/models via load_config) keeps the filtered view."""
    system_cm = _system_config().get("chat_models")
    if not isinstance(system_cm, dict) or not system_cm:
        system_cm = DEFAULTS.get("chat_models") or {}
    user_cm = _load_user_config().get("chat_models")
    merged = _deep_merge(system_cm, user_cm) if isinstance(user_cm, dict) else dict(system_cm)
    return _normalize_chat_models(merged)


def _merged_rich_chat_models(workspace_path: str | None = None) -> dict:
    """The effective chat_models catalog in its RICH on-disk shape (not the
    flattened {key: id} form load_config returns). Mirrors load_config's catalog
    logic exactly — additive SYSTEM+USER deep-merge, user hide-list, project
    wholesale-replace, project hide-list — so the editor's effective view shows
    entries the user can copy down verbatim to override."""
    system_cm = _system_config().get("chat_models")
    if not isinstance(system_cm, dict) or not system_cm:
        system_cm = DEFAULTS.get("chat_models") or {}

    user_stored = _load_user_config()
    user_cm = user_stored.get("chat_models")
    merged = _deep_merge(system_cm, user_cm) if isinstance(user_cm, dict) else dict(system_cm)
    for k in _disabled_keys(user_stored):
        merged.pop(k, None)

    project_stored = _load_project_config(workspace_path)
    proj_cm = project_stored.get("chat_models")
    if isinstance(proj_cm, dict) and proj_cm:
        merged = dict(proj_cm)
    for k in _disabled_keys(project_stored):
        merged.pop(k, None)
    return merged


def effective_config_view(workspace_path: str | None = None) -> dict:
    """A read-only, human-readable view of the fully merged config for the
    editor's 'Effective config (merged)' pane. It is the DEFAULTS→SYSTEM→USER→
    PROJECT→ENV result, but with the RICH chat_models catalog (so a user can copy
    a shipped entry down to override it) and the Tavily secret masked. Never
    written anywhere."""
    merged = dict(load_config(workspace_path))
    merged["chat_models"] = _merged_rich_chat_models(workspace_path)
    merged.pop("chat_model_meta", None)  # derived; noise in a human view
    key = merged.get("tavily_api_key")
    if key:
        merged["tavily_api_key"] = (key[:4] + "..." + key[-4:]) if len(key) > 8 else "***"
    return merged


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
    """Toggle ONE feature flag in the USER config, changing only that boolean and
    preserving every other byte and all formatting.

    A flag toggle should be surgical. Re-serializing the whole file via
    save_config()/json.dump reflows formatting (e.g. collapses aligned
    chat_models one-liners) and flattens the rich chat_models
    shape, silently dropping fields like requires_data_retention. This patches
    the flag's value in place inside the feature_flags object; it only falls
    back to a structured write when the file has no feature_flags block at
    all (rare/first-run). Targets the active user layer (config.user.json, or the
    legacy config.json until the split runs)."""
    literal = "true" if enabled else "false"
    try:
        with open(_active_user_config_path()) as f:
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
