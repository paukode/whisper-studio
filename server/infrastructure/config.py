"""Configuration management with a layered merge and session-latching support.

Config layers (lowest to highest priority):
  1. DEFAULTS — built-in fallbacks (code)
  2. SYSTEM — the bundle's config.example.json at repo_root() (read-only; ships
     inside the .app and updates with every app version). The app-owned model
     catalog + shipped defaults.
  3. USER — config.user.json in the app home (the user owns it; survives app
     updates; the in-settings editor writes here). Holds only the user's deltas:
     added local models, region/keys/flags, per-field overrides, and a hide-list.
     Startup never writes it: the only write the user did not ask for is the
     first-run seed of a home that has no config.user.json. The old
     single-file config.json is never read or changed: while one exists,
     startup shows a notice (old_config_notices).
  4. PROJECT — .whisper/settings.json in the workspace root (per-project)
  5. ENV — sensitive secrets from environment variables

``chat_models`` is an ADDITIVE per-key deep-merge across SYSTEM and USER: the
shipped catalog always appears, and a user entry adds a new key or overrides
specific fields of a shipped entry. ``chat_models_disabled`` (a list of keys in
the USER and PROJECT layers) drops shipped models the user doesn't want after
the merge. The PROJECT layer still replaces the catalog wholesale, matching its
historical semantics. Pricing is not part of this config: it is the shipped
pricing.example.json overlaid by an optional pricing.json of the user's own
per-key overrides (server/costs/tracker.py).

The latching system caches config snapshots per session so that mid-session
settings changes don't disrupt an active conversation or invalidate prompt caches.

Latching fields: bedrock_region, chat_models, default_chat_model, effort_level,
permission_mode. These are frozen at session start and only refresh when a
new session begins.
"""

import functools
import json
import logging
import os
import re
import tempfile
import threading
import time

from fastapi import APIRouter, Request

from server.infrastructure.effort import (
    infer_effort_tier,
    infer_supports_ultracode,
    normalize_effort,
    openai_effort_tier_for,
)
from server.infrastructure.paths import config_dir, repo_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/config", tags=["config"])

# The user's live config follows the app home (WHISPER_HOME in a packaged
# install, the repo root in a dev checkout); the committed template always
# stays with the code.
#
# USER_CONFIG_PATH (config.user.json) is the user layer. LEGACY_CONFIG_PATH
# (config.json) is the old single-file format: never read or changed, only
# checked for so startup can tell the user it is ignored.
USER_CONFIG_PATH = os.path.join(config_dir(), "config.user.json")
LEGACY_CONFIG_PATH = os.path.join(config_dir(), "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(repo_root(), "config.example.json")

# The SYSTEM layer is the full config.example.json (the app-owned catalog +
# shipped defaults). It is read-only in the bundle, so we cache it keyed on the
# file's mtime — a rebuilt bundle (new mtime) is picked up, but steady-state
# loads never re-read from disk.
_system_cache: dict | None = None
_system_cache_key: tuple | None = None


class ConfigFileError(ValueError):
    """A config file exists but cannot be used (not JSON, or not an object).
    The message names the file and where it breaks, so the user can fix it."""


def _read_json_object(path: str) -> dict:
    """Parse ``path`` as a JSON object. FileNotFoundError propagates (the
    caller decides what a missing file means); anything unusable raises
    ConfigFileError instead of reading as an empty config, because a later
    save would then overwrite the file with only the changed keys."""
    with open(path) as f:
        text = f.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigFileError(
            f"{path} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg}). "
            "Fix the file, then restart Whisper Studio."
        ) from None
    if not isinstance(data, dict):
        raise ConfigFileError(f"{path} must hold a JSON object.")
    return data


def _system_config() -> dict:
    """The full SYSTEM layer: config.example.json parsed as a config dict.
    A missing or broken template is a broken install and raises.
    Cached on (path, mtime) so tests that swap EXAMPLE_CONFIG_PATH still work."""
    global _system_cache, _system_cache_key
    path = EXAMPLE_CONFIG_PATH
    try:
        key = (path, os.path.getmtime(path))
    except FileNotFoundError:
        raise ConfigFileError(
            f"{path} is missing: the install is incomplete. Reinstall Whisper Studio."
        ) from None
    if _system_cache is not None and _system_cache_key == key:
        return _system_cache
    data = _read_json_object(path)
    _system_cache = data
    _system_cache_key = key
    return data


DEFAULTS = {
    # Where app data (result cache, JSON stores, DBs) lives. Empty = the
    # default <repo>/data. Overridable so a packaged/worktree install can pin
    # a stable location; see server/infrastructure/paths.py.
    "data_dir": "",
    "tavily_api_key": "",
    # The transcript panel's Translate dropdown: "off", "canary" (Canary's
    # native translation, any engine's audio, 25 languages with English on
    # one side of the pair), or "apple" (macOS on-device translator via the
    # app shell, any pair among its ~20 languages).
    "translate_mode": "off",
    # Target language for the translation line (ISO 639-1).
    "translate_target": "en",
    # First-run model-mode notice: flipped true when the user dismisses it.
    # Lives in config (not browser localStorage) because the Mac app shell
    # serves the SPA from a fresh localhost origin whenever its port changes,
    # which would wipe a localStorage flag and re-show the notice every launch.
    "mode_notice_seen": False,
    # Which ASR backend the live recorder uses:
    #   "streaming" — alias for the Parakeet backend, word-by-word interims (default)
    #   "whisper"   — utterance-based mlx-whisper (proven fallback path)
    # Resolved via the registry in server/asr. Read once at /ws connect so
    # a mid-recording change doesn't swap models on a live session.
    "transcription_backend": "streaming",
    # ── Speaker attribution ──
    # Which speaker encoder produces the embeddings behind "Speaker N":
    #   "redimnet" — ReDimNet2-B2, 0.57% EER on VoxCeleb1-O (default)
    #   "ecapa"    — ECAPA-VoxCeleb, the previous encoder, ~1% EER
    # ReDimNet2 downloads on first use and falls back to ECAPA if it can't.
    # Changing this invalidates stored voiceprints (vectors from different
    # encoders are not comparable), which the gallery files per encoder.
    "speaker_embedder": "redimnet",
    # Cut a VAD utterance at speaker handovers instead of labelling the
    # whole thing as one speaker. Needs word timestamps from the engine
    # (Whisper and Parakeet both report them).
    "speaker_split_turns": True,
    # Remember named speakers across recordings. Renaming "Speaker 2" to a
    # name stores that cluster's voiceprint; later sessions recognise them.
    # Opt-in: matching a voice across sessions is not reliable enough to put
    # a name on somebody by default, so speakers start as "Speaker 1/2" every
    # recording unless this is turned on explicitly.
    "speaker_voiceprints": False,
    "bedrock_region": "us-east-1",
    # Hands-free voice mode (Amazon Nova 2 Sonic over Bedrock). region "" follows
    # bedrock_region; Sonic is only in us-east-1, us-west-2, eu-north-1 and
    # ap-northeast-1. Effective values resolve in server/voice/routes.py.
    "voice": {
        "enabled": True,
        "model_id": "amazon.nova-2-sonic-v1:0",
        "region": "",
        "voice_id": "tiffany",
        "endpointing": "MEDIUM",
    },
    # Chat-model catalog. The app-owned catalog lives in config.example.json (the
    # SYSTEM layer); the empty entry here only marks chat_models as a settable
    # key (PUT /api/config and config_set write DEFAULTS keys). SYSTEM and USER deep-merge ADDITIVELY per key (see load_config): the shipped
    # catalog always appears and a USER entry adds a key or overrides fields of a
    # shipped one. To add/change a shipped model, edit config.example.json; users
    # add their own via config.user.json. Shape {key: {id, label, ...}}.
    "chat_models": {},
    # Model keys to HIDE from the effective catalog after the SYSTEM+USER merge.
    # Lets a user drop a shipped model they don't want without deleting it from
    # the app-owned catalog. Honored in the USER and PROJECT layers (unioned);
    # unknown names are ignored. Default [] = show system models + the user's own.
    "chat_models_disabled": [],
    "default_chat_model": "gpt6-luna",
    "permission_mode": "default",  # default | auto | plan | acceptEdits | bypassPermissions | dontAsk
    "permission_explainer_enabled": True,
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
    # The one round limit and time limit every run takes (Settings > Costs >
    # Budget); who gets which, and the exceptions: server/infrastructure/run_limits.py.
    "round_limit": 120,
    "time_limit_minutes": 15,
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
    # Side-task model routing: {task: chat_models key | "main"}. Empty keeps every
    # task on its historical default (Haiku for cheap judges and selectors, the
    # session model for compaction and the learning review). Tasks are listed in
    # server/infrastructure/auxiliary.py.
    "auxiliary_models": {},
    # Per-turn tool-call caps for the loop guard, overlaying
    # server/chat/loop_guard.py DEFAULT_CAPS ({tool_name: max calls, 0 = off}).
    "tool_loop_caps": {},
    # When agent reports land in a session with no turn running (a cancelled
    # team, a background or resumed agent), start a synthesis turn on the
    # session's model that answers from them (server/agents/wake.py).
    "wake_parent_on_reports": True,
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
        "permission_mode",
    }
)

# Keys allowed in project-level .whisper/settings.json
# (subset of DEFAULTS — exclude global-only keys like API keys)
#
# The permission mode and the auto-mode classifier rules are deliberately NOT
# here: a workspace is untrusted input (a cloned repo, a folder a tool wrote
# with no card in acceptEdits or bypass), so a project file must not be able
# to put an attended run into bypass (server/exec/headless.py reads
# permission_mode from the merged config) or neuter the auto classifier
# (server/auto_mode.py reads the auto_mode_* rules from it). Those stay a
# user-level decision only. permission_explainer_enabled only toggles an
# advisory explanation, so it is harmless to keep.
PROJECT_SETTINGS_KEYS = frozenset(
    {
        "bedrock_region",
        "chat_models",
        "chat_models_disabled",
        "default_chat_model",
        "effort_level",
        "permission_explainer_enabled",
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

# ── User-layer writes ────────────────────────────────────────────────
# Held across every read-modify-write of the USER layer (read, change, save as
# one step) and by every write. Its writers run on the event loop (PUT
# /api/config, the Settings toggles) and on worker threads (a Discover install
# finalising, the config tool), so without one lock a write that lands between
# another writer's read and its save is silently lost. Re-entrant, so a held
# span can call save_config. Held only for a small file read and write, never
# across a network call or an await.
USER_CONFIG_LOCK = threading.RLock()


def edits_user_config(fn):
    """Decorator: run ``fn`` (a whole read-modify-write of the USER layer)
    holding USER_CONFIG_LOCK, so no other writer lands between its read and its
    save."""

    @functools.wraps(fn)
    def _locked(*args, **kwargs):
        with USER_CONFIG_LOCK:
            return fn(*args, **kwargs)

    return _locked


def _as_int(v) -> int | None:
    """Coerce a config-supplied number to int, or None if absent/malformed."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Model-id markers of the 1M-context Claude family (Fable 5, Mythos, Opus 5
# and 4.6/4.7/4.8, Sonnet 5 and 4.6). Anything unrecognized keeps the safe
# 200K family default (server/chat/engine/windows.py reads this tuple).
ANTHROPIC_1M_MARKERS = (
    "fable-5",
    "mythos",
    "opus-5",
    "opus-4-6",
    "opus-4-7",
    "opus-4-8",
    "sonnet-5",
    "sonnet-4-6",
)


def _lift_stale_context_window(value: int | None, model_id: str) -> int | None:
    """Upgrade-on-read for a stale template default.

    The example config shipped ``context_window: 200000`` on every Claude
    entry from the era when Bedrock was assumed to cap prompts at 200K.
    Verified 2026-08-29 (live probe: a 312K-token prompt accepted on
    claude-sonnet-5; 1M context is GA on Bedrock): the current 1M-family
    Claude models take the full window, so a configured 200000 on those ids
    reads as 1,000,000. Any OTHER explicit value is a deliberate per-model
    choice and stands, as does 200000 on non-1M models (Haiku)."""
    if value != 200_000:
        return value
    mid = (model_id or "").lower()
    if any(marker in mid for marker in ANTHROPIC_1M_MARKERS):
        return 1_000_000
    return value


# The us-west-2 pin GPT-6 Astra shipped with (2.7.0 to 2.11.2), from when
# bedrock-mantle served Astra only there. It serves Astra in us-east-1 too
# (measured 2026-10-03), so Astra follows bedrock_region like every other model.
# config.user.json keeps whole entries, so a saved copy of the old entry still
# carries that pin: read there, it is no pin at all.
_RETIRED_REGION_PINS = {"openai.gpt-6-astra": "us-west-2"}


def _drop_retired_pin(region: str | None, model_id: str) -> str | None:
    """Upgrade-on-read for a region pin the app itself once shipped (see
    _RETIRED_REGION_PINS). Any other pin is a deliberate per-model choice and
    stands."""
    retired = _RETIRED_REGION_PINS.get((model_id or "").strip().lower())
    if isinstance(region, str) and retired and region.strip() == retired:
        return None
    return region


def _normalize_chat_models(chat_models: dict) -> tuple[dict, dict]:
    """Split a chat_models map into (ids, meta).

    Each entry is an object: {"opus5.5": {"id": "...", "label": "Opus 5.5"}}.
    An entry without an "id" string raises ConfigFileError.

    Returns:
        ids:  {key: bedrock_model_id}
        meta: {key: {id, label, provider, ...}} for /api/models and the engine
    """
    ids: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for key, val in chat_models.items():
        if not (isinstance(val, dict) and isinstance(val.get("id"), str)):
            raise ConfigFileError(f'chat_models.{key} must be an object with an "id" string.')
        model_id = val["id"]
        ids[key] = model_id
        # Inference provider. "anthropic" (default) goes through the
        # bedrock-runtime Converse path; "openai_bedrock" routes to the
        # OpenAI Responses API on bedrock-mantle (server/openai_bedrock).
        # Explicit wins; otherwise inferred from the model id so an OpenAI
        # model added to config is as turnkey as a Claude one: just give it
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
            # Mythos-class models (e.g. Fable 5) require the AWS account's
            # Bedrock data-retention mode to be provider_data_share. The UI
            # uses this flag to gate model selection behind a consent screen.
            "requires_data_retention": bool(val.get("requires_data_retention", False)),
            # On-device model: runs via the local runtime,
            # not Bedrock. Surfaced on /api/models for the picker badge.
            "is_local": bool(val.get("is_local", False)),
            # Whether this local model can use tools (local agentic loop).
            "supports_tools": bool(val.get("supports_tools", False)),
            # Which effort levels this model exposes (full/standard/none/
            # openai/openai6). Explicit wins; OpenAI models default to their
            # generation's ladder (GPT-6 has no "none" rung), everything else
            # infers from the key. Drives the per-model effort picker and the
            # Bedrock output_config.effort.
            "effort_tier": val.get("effort_tier")
            or (
                openai_effort_tier_for(model_id)
                if provider == "openai_bedrock"
                else infer_effort_tier(key, model_id)
            ),
            # Whether this model can drive workflow orchestration. Kept
            # SEPARATE from effort_tier: the tier says which raw reasoning
            # values the provider accepts, this says whether the model can
            # author and run a workflow. Sonnet 5 orchestrates on a standard
            # ladder. None ⇒ inferred (see effort.supports_ultracode).
            "supports_ultracode": val.get("supports_ultracode"),
            "provider": provider,
            # OpenAI-on-Bedrock only: an optional per-model region override
            # (absent ⇒ the account-wide bedrock_region is used, same as the
            # Anthropic path) and the GPT verbosity (text.verbosity).
            # Ignored by other providers.
            "openai_region": _drop_retired_pin(val.get("openai_region"), model_id),
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
            # Which on-device engine serves the weights: absent/None ⇒ GGUF
            # via llama-server; "mlx" ⇒ a whole-repo snapshot via mlx_lm
            # (no filename). Dropping this here would silently reject every
            # MLX entry downstream (the registry would demand a filename).
            "engine": val.get("engine"),
            # Context accounting (turn engine): the model's total input
            # window and max output tokens. Absent ⇒ per-family defaults
            # (see server/chat/engine/windows.py); local models resolve
            # from the live n_ctx instead.
            "context_window": _lift_stale_context_window(
                _as_int(val.get("context_window")), model_id
            ),
            "max_output": _as_int(val.get("max_output")),
        }
        # When the entry does not declare orchestration capability, infer it
        # from the actual model id via effort.infer_supports_ultracode so raw
        # load_config() consumers match startup behavior. Explicit true/false
        # values pass through untouched.
        entry = meta[key]
        if entry.get("supports_ultracode") is None:
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


def _user_config_path() -> str:
    """The user layer file, config.user.json. Reads the module attribute on each
    call so tests can point it elsewhere."""
    return USER_CONFIG_PATH


def _load_user_config() -> dict:
    """Load the USER layer (config.user.json) from disk: {} when there is no
    file. A file that is not a JSON object raises ConfigFileError."""
    try:
        return _read_json_object(_user_config_path())
    except FileNotFoundError:
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
        stored = _read_json_object(settings_path)
    except FileNotFoundError:
        return {}
    # Only allow project-safe keys
    return {k: v for k, v in stored.items() if k in PROJECT_SETTINGS_KEYS}


def _env_overlay() -> dict:
    """Read sensitive secrets from environment variables. These override
    anything stored in config.user.json so users can keep keys out of disk
    (e.g. export TAVILY_API_KEY=... in their shell rc, rotate via env
    without re-editing config). Empty env values are ignored — config
    falls back to whatever's in DEFAULTS or config.user.json.
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

    # Layers 1-3: DEFAULTS + SYSTEM (config.example.json) + USER
    # (config.user.json). Cached together.
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
        # overrides individual fields of a shipped one.
        user_merged = _deep_merge(DEFAULTS, system_stored)
        user_merged = _deep_merge(user_merged, user_stored)
        # Hide-list: drop the models the USER asked to hide from the merged
        # catalog. (Project adds its own below.)
        _apply_disabled(user_merged, _disabled_keys(user_stored))
        with _config_cache_lock:
            _config_cache = user_merged
            _config_cache_mtime = now

    # Layer 4: project config (not cached — workspace can change)
    project_stored = _load_project_config(workspace_path)
    if project_stored:
        _project_retired_value_notices(workspace_path, project_stored)
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
    # to config.user.json; see update_config below for the save path.
    if "chat_models" in merged:
        ids, meta = _normalize_chat_models(merged["chat_models"])
        merged["chat_models"] = ids
        merged["chat_model_meta"] = meta

    # An unknown effort value (the retired "auto" included) runs as the default
    # level, so downstream code and the per-model clamp never see it. The saved
    # value stays in the user's file; old_config_notices names it.
    if "effort_level" in merged:
        merged["effort_level"] = normalize_effort(merged.get("effort_level"))

    # A value the app no longer offers runs as its replacement, in memory only:
    # the user's file keeps what they saved, and old_config_notices says so.
    for key, replacements in RETIRED_USER_VALUES.items():
        value = merged.get(key)
        if isinstance(value, str) and value in replacements:
            merged[key] = replacements[value][0]

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
    """Persist the USER layer (config.user.json). A caller that read the layer
    to build ``config`` holds USER_CONFIG_LOCK across that read and this save."""
    with USER_CONFIG_LOCK:
        _atomic_write(_user_config_path(), json.dumps(config, indent=2))
        _invalidate_cache()


def _write_config_text(text: str) -> None:
    """Persist raw USER-layer text ATOMICALLY, then drop the in-memory cache so
    the next load_config() sees the new contents. Targets config.user.json."""
    with USER_CONFIG_LOCK:
        _atomic_write(_user_config_path(), text)
        _invalidate_cache()


# Written into a brand-new config.user.json on first launch (packaged app, or
# any fresh home). After the file exists it is never overwritten, so the user's
# saved settings win from the second launch on. Defaults: LOCAL mode — nothing
# leaves the machine until the user opts into hybrid or cloud (the first-run
# notice in the UI explains the three modes). Transcription works out of the
# box (engines download on demand; the language-ID and speaker models ship
# bundled); chat needs a local model from Settings > Models > Discover, which
# the notice points at. The ``backends`` map below is dormant in local mode
# and takes effect when the user switches to hybrid: on-device embeddings +
# reranker (qwen3) and GLiNER entity extraction, with the one-shot writer /
# relationship mapping (``index_llm``) on cloud Haiku — the app ships NO local
# chat model, so a local index_llm key would point at a missing model. Once
# the user installs a local chat model from Discover, the install flips
# index_llm to it (server/model_browser/service.py).
FIRST_RUN_USER_CONFIG = {
    "model_mode": "local",
    "backends": {
        "embed": "qwen3",
        "rerank": "qwen3",
        "ner": "gliner",
        "index_llm": "haiku",
    },
}


def seed_user_config() -> bool:
    """Write FIRST_RUN_USER_CONFIG into a new config.user.json on a fresh home.

    Does nothing when config.user.json already exists: the user's file is never
    overwritten. A home with only an old config.json counts as fresh: that file
    mixed shipped values with the user's own, so nothing is carried over from
    it, and it stays as it is (old_config_notices tells the user). Returns True
    only when it wrote the file; a failed write raises.
    """
    # Resolve the home at CALL time (not the import-frozen module constants) so a
    # packaged boot seeds within the live WHISPER_HOME even if the module was
    # imported before the env was set.
    user_path = os.path.join(config_dir(), "config.user.json")
    if os.path.exists(user_path):
        return False
    _atomic_write(user_path, json.dumps(FIRST_RUN_USER_CONFIG, indent=2) + "\n")
    _invalidate_cache()
    return True


def _value_checks() -> dict[str, tuple[set[str], str]]:
    """Settings with a fixed set of values: {key: (known values, what runs when
    the saved value is not one of them)}. The readers fall back on their own
    (server/asr resolve_name, model_mode.current_mode, the translator pick,
    effort.normalize_effort);
    this only lets startup say so. Imported here, not at module level, to keep
    config importable first."""
    from server.asr import _ALIASES, BACKENDS, DEFAULT_BACKEND
    from server.infrastructure.effort import _LABEL_ALIASES, DEFAULT_EFFORT, EFFORT_ORDER
    from server.infrastructure.model_mode import DEFAULT_MODE, MODES

    return {
        "transcription_backend": (
            {*BACKENDS, *_ALIASES},
            f"{DEFAULT_BACKEND.capitalize()} transcribes",
        ),
        "translate_mode": ({"off", "canary", "apple"}, "no translation line is shown"),
        "model_mode": (set(MODES), f"the app runs in {DEFAULT_MODE} mode"),
        "effort_level": ({*EFFORT_ORDER, *_LABEL_ALIASES}, f"{DEFAULT_EFFORT} effort is used"),
    }


def old_config_notices() -> list[tuple[str, str]]:
    """(title, message) for each old or unknown setting the app does not use, so
    startup says so instead of ignoring it quietly. Reads only; nothing on disk
    changes. Each message stays the same while its cause does, so the bell shows
    it once (record_notification_once).

    - An old single-file config.json next to config.user.json.
    - local_mode set in config.user.json with no model_mode: local_mode used to
      switch the app to Local mode, so the user would otherwise get the shipped
      mode without being told.
    - RETIRED_USER_KEYS still in config.user.json: they have no effect.
    - A RETIRED_USER_VALUES value in config.user.json: load_config runs its
      replacement instead.
    - A value outside the known set of a _value_checks() setting.
    """
    shipped_mode = str(_system_config().get("model_mode") or DEFAULTS["model_mode"])
    notices: list[tuple[str, str]] = []
    if os.path.exists(LEGACY_CONFIG_PATH):
        notices.append(
            (
                "config.json is no longer read",
                f"Whisper Studio ignores {LEGACY_CONFIG_PATH} and leaves it as it is. "
                f"Settings are read from {USER_CONFIG_PATH} only. Copy what you still "
                "need from config.json into config.user.json, or set it again in "
                "Settings, then delete or rename config.json.",
            )
        )
    user = _load_user_config()
    if user.get("local_mode") and "model_mode" not in user:
        notices.append(
            (
                "local_mode is no longer read",
                f"{USER_CONFIG_PATH} sets local_mode, which the app no longer reads, and "
                f"no model_mode, so the app runs in {shipped_mode} mode. If you want Local "
                "mode, choose it in Settings; otherwise remove local_mode from that file.",
            )
        )
    old_keys = [key for key in RETIRED_USER_KEYS if key in user]
    if old_keys:
        reasons = "; ".join(f"{key} ({RETIRED_USER_KEYS[key]})" for key in old_keys)
        notices.append(
            (
                "Old settings have no effect",
                f"{USER_CONFIG_PATH} still has settings the app no longer reads: "
                f"{reasons}. The app leaves your file as it is; remove them from it "
                "when you like.",
            )
        )
    for key, replacements in RETIRED_USER_VALUES.items():
        value = user.get(key)
        if isinstance(value, str) and value in replacements:
            notices.append(
                (
                    f'{key} "{value}" is no longer offered',
                    f'{USER_CONFIG_PATH} sets {key} to "{value}": '
                    f"{replacements[value][1]}. The app leaves your file as it is. "
                    f"Choose again in Settings, or change {key} in that file, to save "
                    "your own choice.",
                )
            )
    unknown = []
    for key, (known, fallback) in _value_checks().items():
        if key not in user:
            continue
        value = user[key]
        if isinstance(value, str) and value in RETIRED_USER_VALUES.get(key, {}):
            continue  # named above, with its replacement
        name = None
        if isinstance(value, str):
            # resolve_name lowercases and strips the engine name; the others
            # compare the saved text as it is.
            name = value.strip().lower() if key == "transcription_backend" else value
        if name not in known:
            unknown.append(f"{key} {json.dumps(value)} ({fallback})")
    if unknown:
        notices.append(
            (
                "Unknown setting values",
                f"{USER_CONFIG_PATH} has values the app does not know: "
                f"{'; '.join(unknown)}. The app leaves your file as it is. Choose "
                "again in Settings, or fix them in that file.",
            )
        )
    return notices


# Keys the app no longer reads, with what replaced each: the per-type agent
# budgets and the scheduled-run round cap gave way to the one round limit and
# time limit every run takes; the transcription languages gave way to each
# engine's own detection; brief replies are chosen per message. A saved one
# stays in config.user.json (startup never edits the user's file) and
# old_config_notices names it.
RETIRED_USER_KEYS = {
    "agent_limits": "the round limit and time limit in Settings replace it",
    "cron_max_rounds": "the round limit and time limit in Settings replace it",
    "whisper_language": "each transcription engine detects the language itself",
    "brief_mode": "brief replies are chosen for each message in the chat",
}

# Values the app no longer offers: {key: {old value: (what runs instead, why)}}.
# load_config applies the replacement in memory; the saved value stays in
# config.user.json and old_config_notices names it.
_OLD_TRANSLATE = (
    "off",
    "the old automatic translation is gone, so no translation line is shown; "
    "choose Canary or Apple in the Translate menu",
)
RETIRED_USER_VALUES = {
    "transcription_backend": {
        "canary": ("streaming", "Canary only translates now, so Parakeet transcribes instead"),
    },
    "translate_mode": {"auto": _OLD_TRANSLATE, "model": _OLD_TRANSLATE},
    "effort_level": {
        "auto": ("high", "the automatic effort level is gone, so high effort is used"),
    },
}


def _project_retired_value_notices(workspace_path: str | None, project: dict) -> None:
    """Say so, once, when a project's .whisper/settings.json sets a value the
    app no longer offers (effort_level "auto"): load_config runs the same
    replacement as for the user's file, which old_config_notices names at
    startup. The project file is read on every load, so the notice goes
    through old_format_notice (once per process, once in the bell). Nothing
    on disk changes."""
    from server.notifications import old_format_notice

    settings_path = os.path.join(workspace_path or "", ".whisper", "settings.json")
    for key, replacements in RETIRED_USER_VALUES.items():
        value = project.get(key)
        if isinstance(value, str) and value in replacements:
            old_format_notice(
                title=f'{key} "{value}" is no longer offered',
                message=(
                    f'{settings_path} sets {key} to "{value}": '
                    f"{replacements[value][1]}. The app leaves your file as it is. "
                    f"Change {key} in that file to save your own choice."
                ),
            )


def unfiltered_chat_models() -> tuple[dict, dict]:
    """The normalized ``(ids, meta)`` of the DEFAULTS→SYSTEM→USER chat catalog
    WITHOUT the ``chat_models_disabled`` hide-list applied.

    Weight-management surfaces read this instead of load_config(): the local
    registry (and through it the Settings > Models download manager) must keep
    serving a disabled model so its weights stay manageable — disabling only
    hides a model from the composer picker, it never strands gigabytes on disk.
    The picker path (/api/models via load_config) keeps the filtered view."""
    system_cm = _system_config().get("chat_models") or {}
    user_cm = _load_user_config().get("chat_models")
    merged = _deep_merge(system_cm, user_cm) if isinstance(user_cm, dict) else dict(system_cm)
    return _normalize_chat_models(merged)


def _merged_rich_chat_models(workspace_path: str | None = None) -> dict:
    """The effective chat_models catalog in its RICH on-disk shape (not the
    flattened {key: id} form load_config returns). Mirrors load_config's catalog
    logic exactly — additive SYSTEM+USER deep-merge, user hide-list, project
    wholesale-replace, project hide-list — so the editor's effective view shows
    entries the user can copy down verbatim to override."""
    system_cm = _system_config().get("chat_models") or {}

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


@edits_user_config
def set_feature_flag(flag_name: str, enabled: bool) -> None:
    """Toggle ONE feature flag in the USER config, changing only that boolean and
    preserving every other byte and all formatting.

    A flag toggle should be surgical. Re-serializing the whole file via
    save_config()/json.dump reflows formatting (e.g. collapses aligned
    chat_models one-liners) and flattens the rich chat_models
    shape, silently dropping fields like requires_data_retention. This patches
    the flag's value in place inside the feature_flags object; it only falls
    back to a structured write when the file has no feature_flags block at
    all (rare/first-run). Targets the user layer (config.user.json)."""
    literal = "true" if enabled else "false"
    try:
        with open(_user_config_path()) as f:
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


@edits_user_config
def _apply_config_update(body: dict) -> None:
    """Overlay ``body``'s known keys on the RAW user layer and save it, as one
    locked read-modify-write."""
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


@router.put("")
async def update_config(request: Request):
    body = await request.json()
    # Use the RAW on-disk config as the base, not load_config(). load_config
    # flattens chat_models for downstream callers; if we wrote that back we'd
    # lose the rich {id, label, thinking} shape and the per-model metadata.
    # Body fields overlay on top; everything else is preserved verbatim.
    _apply_config_update(body)
    log.info("Config updated: %s", list(body.keys()))
    return {"updated": True}
