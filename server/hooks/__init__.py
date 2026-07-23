"""Blocking hooks: PreToolUse can deny or rewrite a tool call, PostToolUse can
feed context back, Stop can refuse to end a turn. One engine, every model path.

Public surface consumed by the tool executor, the end-of-turn gates (WS-E), the
routes layer, and plugins.
"""

from server.hooks.config_loader import (
    approve_project_hooks,
    load_user_hooks,
    merged_for_event,
    project_trust_status,
    save_user_hooks,
)
from server.hooks.engine import (
    MAX_STOP_BLOCKS_PER_TURN,
    check_stop_hooks,
    dry_run,
    run_hooks,
)
from server.hooks.schema import (
    HOOK_EVENTS,
    HookDef,
    HookOutcome,
    build_stdin_payload,
    canonical_event,
    matches,
    normalize_config,
    serialize_v2,
)

__all__ = [
    "HOOK_EVENTS",
    "MAX_STOP_BLOCKS_PER_TURN",
    "HookDef",
    "HookOutcome",
    "approve_project_hooks",
    "build_stdin_payload",
    "canonical_event",
    "check_stop_hooks",
    "dry_run",
    "load_user_hooks",
    "matches",
    "merged_for_event",
    "normalize_config",
    "project_trust_status",
    "run_hooks",
    "save_user_hooks",
    "serialize_v2",
]
