"""Feature flags — runtime gating for Whisper features.

Provides a lightweight feature flag system backed by config.json.
Flags can be checked at runtime to gate features on/off without code changes.

Usage:
    from server.infrastructure.feature_flags import is_enabled, get_flag, FlagRegistry

    if is_enabled("auto_memory"):
        # memory feature code
        ...

    # Get flag with metadata
    flag = get_flag("auto_memory")
    if flag and flag.enabled:
        ...
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("whisper-studio")


@dataclass(frozen=True)
class FeatureFlag:
    """A single feature flag definition."""

    name: str
    description: str
    default: bool = False
    category: str = "general"


# Central registry of all feature flags with their metadata.
# Adding a flag here automatically makes it available in config.json.
_FLAG_DEFINITIONS: dict[str, FeatureFlag] = {}
_lock = threading.Lock()


def register_flag(
    name: str,
    *,
    description: str,
    default: bool = False,
    category: str = "general",
) -> FeatureFlag:
    """Register a feature flag. Idempotent — re-registering with the same name updates the definition."""
    flag = FeatureFlag(name=name, description=description, default=default, category=category)
    with _lock:
        _FLAG_DEFINITIONS[name] = flag
    return flag


def get_all_flags() -> dict[str, FeatureFlag]:
    """Return a copy of the flag registry."""
    with _lock:
        return dict(_FLAG_DEFINITIONS)


def get_flag_defaults() -> dict[str, bool]:
    """Return {flag_name: default_value} for all registered flags."""
    with _lock:
        return {name: f.default for name, f in _FLAG_DEFINITIONS.items()}


def is_enabled(name: str) -> bool:
    """Check if a feature flag is enabled.

    Resolution order:
    1. config.json feature_flags.<name> (user override)
    2. Registered default value
    3. False (unknown flags are off)
    """
    from server.infrastructure.config import load_config

    config = load_config()
    flags_config = config.get("feature_flags", {})
    if name in flags_config:
        return bool(flags_config[name])
    with _lock:
        defn = _FLAG_DEFINITIONS.get(name)
    if defn:
        return defn.default
    return False


def get_flag(name: str) -> FeatureFlag | None:
    """Get a flag definition by name."""
    with _lock:
        return _FLAG_DEFINITIONS.get(name)


def get_flag_states() -> dict[str, dict[str, Any]]:
    """Return all flags with their current resolved state.

    Returns a dict of {name: {enabled, default, description, category, source}}.
    """
    from server.infrastructure.config import load_config

    config = load_config()
    flags_config = config.get("feature_flags", {})

    result = {}
    with _lock:
        for name, defn in _FLAG_DEFINITIONS.items():
            if name in flags_config:
                enabled = bool(flags_config[name])
                source = "config"
            else:
                enabled = defn.default
                source = "default"
            result[name] = {
                "enabled": enabled,
                "default": defn.default,
                "description": defn.description,
                "category": defn.category,
                "source": source,
            }
    return result


# ── Built-in flag registrations ──────────────────────────────────────
# Register all Whisper feature flags here. These serve as the canonical
# list of gated features and their defaults.

register_flag(
    "companion",
    description="Show the cosmetic buddy companion in the chat corner",
    # Default OFF — opt-in delight, not a default. The companion is now
    # purely cosmetic (animated SVG, client-rendered): it makes no LLM calls
    # and never touches a chat turn. The flag just controls whether the
    # widget is shown at all.
    default=False,
    category="ui",
)

register_flag(
    "git_context",
    description="Include git status and instructions in system prompt",
    default=True,
    category="workspace",
)

register_flag(
    "prompt_caching",
    description="Cloud (Bedrock) prompt caching of tool definitions + static system prompt to cut input-token cost on multi-turn conversations",
    default=True,
    category="chat",
)

register_flag(
    "progressive_tools",
    description="Progressive tool disclosure: advertise a core tool set plus session-activated tools; everything else is discoverable via tool_search from a compact index (cuts ~60-75% of tool-schema tokens per turn)",
    default=True,
    category="chat",
)

register_flag(
    "strict_rag",
    description="When index grounding is injected, withhold the workspace file/search tools (semantic_search, ws_read_file, ws_grep, ws_glob, ws_list_directory) so the model answers from the retrieved passages instead of re-crawling files",
    default=True,
    category="chat",
)

register_flag(
    "rag_hybrid_search",
    description="Hybrid retrieval: fuse keyword/BM25 (SQLite FTS5) results with the dense vector search via reciprocal-rank fusion, so exact terms the embedding misses (filenames, ids, codes) still rank. Builds from the existing index (no re-index needed); default on",
    default=True,
    category="chat",
)

register_flag(
    "rag_reranker",
    description="Cross-encoder reranking: reorder the fused retrieval candidates by judging each (question, passage) pair directly, before grounding's top-k. Cloud mode uses Cohere Rerank 3.5 (Bedrock); local mode uses Qwen3-Reranker-0.6B (~2.4GB, first grounded turn cold-loads it). Default on; toggle in the Feature Flags settings.",
    default=True,
    category="chat",
)

register_flag(
    "rag_query_rewrite",
    description="Tier 3 retrieval: use a fast LLM (Haiku) to rewrite a follow-up question into a standalone, context-complete search query before grounding. When on, this replaces the default heuristic contextualization + dual-query fusion. Adds one cheap model call per grounded turn; default off",
    default=False,
    category="chat",
)

register_flag(
    "auto_memory",
    description=(
        "Two-tier auto memory with extraction and recall: global "
        "(cross-workspace, works in plain chat) + project (workspace-scoped)"
    ),
    default=True,
    category="memory",
)

register_flag(
    "session_memory",
    description="Per-session structured summaries injected into prompt",
    default=True,
    category="memory",
)

register_flag(
    "goal_loop",
    description=(
        "Goal loop + completion gate: a session goal auto-continues the turn "
        "until a cheap evaluator judges it achieved (capped at 8 blocks)"
    ),
    default=True,
    category="agent",
)

register_flag(
    "cron_verify",
    description="Verify a cron run achieved its prompt before pushing status ok",
    default=True,
    category="agent",
)

register_flag(
    "dream_consolidation",
    description="Cross-session memory distillation after 24h and 5 sessions",
    default=True,
    category="memory",
)

register_flag(
    "preview_tools",
    description=(
        "Assistant-controllable browser preview tools (preview_start/stop/click/"
        "fill/eval/screenshot/etc.) — spawns a dev-server subprocess and drives it "
        "with a headless Playwright browser. On by default; the tools only surface "
        "once Playwright + Chromium are present (setup.sh installs them) and hide "
        "themselves automatically if that install is missing. Toggle off in Settings."
    ),
    default=True,
    category="preview",
)


# ── API ──────────────────────────────────────────────────────────────

from fastapi import APIRouter, Request  # noqa: E402

router = APIRouter(prefix="/api/feature-flags", tags=["feature-flags"])


@router.get("")
async def list_flags():
    """List all feature flags with their current state."""
    return get_flag_states()


@router.put("/{flag_name}")
async def toggle_flag(flag_name: str, request: Request):
    """Enable or disable a feature flag."""
    body = await request.json()
    enabled = body.get("enabled")
    if enabled is None:
        return {"error": "enabled field required"}

    defn = get_flag(flag_name)
    if not defn:
        return {"error": f"Unknown flag: {flag_name}"}

    # Surgical, format-preserving toggle — flips only this flag's boolean in
    # config.json, leaving all other content and formatting byte-for-byte
    # intact (and never flattening the rich chat_models shape).
    from server.infrastructure.config import set_feature_flag

    set_feature_flag(flag_name, bool(enabled))
    log.info("Feature flag '%s' set to %s", flag_name, enabled)
    return {"flag": flag_name, "enabled": bool(enabled)}
