"""Dream consolidation — cross-session memory distillation.

Triggered after 24h + 5 turn-ends since last consolidation, per tier:
each memory dir (global and every project) carries its own
.dream_meta.json counters. Runs a 4-phase agent:
Orient -> Gather -> Consolidate -> Prune.
Uses PID-based lock to prevent concurrent consolidations.

Entry point for callers is ``record_and_maybe_dream`` (post-turn,
fire-and-forget): it bumps the per-tier turn-end counter and spawns the
consolidation agent when a tier is due.
"""

import json
import logging
import os
import time

from server.memory.memdir import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    ensure_global_memory_dir,
    ensure_memory_dir,
)
from server.memory.prompts import CONSOLIDATION_PROMPT

log = logging.getLogger("whisper-studio")

# Trigger thresholds
MIN_HOURS_SINCE_LAST = 24
MIN_SESSIONS_SINCE_LAST = 5

# Lock stale timeout
LOCK_STALE_SECONDS = 3600  # 1 hour

LOCK_FILENAME = ".dream.lock"
META_FILENAME = ".dream_meta.json"


def _lock_path(memory_dir: str) -> str:
    return os.path.join(memory_dir, LOCK_FILENAME)


def _meta_path(memory_dir: str) -> str:
    return os.path.join(memory_dir, META_FILENAME)


def _load_meta(memory_dir: str) -> dict:
    """Load dream metadata. Returns defaults if not found."""
    path = _meta_path(memory_dir)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"last_consolidated_at": 0, "session_count": 0}


def _save_meta(memory_dir: str, meta: dict) -> None:
    path = _meta_path(memory_dir)
    try:
        with open(path, "w") as f:
            json.dump(meta, f)
    except OSError as e:
        log.warning("Failed to save dream meta: %s", e)


def record_session(memory_dir: str) -> None:
    """Increment the per-tier turn-end counter. Called by the post-turn hook
    after every completed chat turn, so ``session_count`` counts turn-ends,
    not whole sessions."""
    meta = _load_meta(memory_dir)
    meta["session_count"] = meta.get("session_count", 0) + 1
    _save_meta(memory_dir, meta)


def should_consolidate(memory_dir: str) -> bool:
    """Check if consolidation conditions are met (24h + 5 turn-ends)."""
    from server.infrastructure.feature_flags import is_enabled

    if not is_enabled("dream_consolidation"):
        return False

    meta = _load_meta(memory_dir)
    last_at = meta.get("last_consolidated_at", 0)
    sessions = meta.get("session_count", 0)

    hours_since = (time.time() - last_at) / 3600
    return hours_since >= MIN_HOURS_SINCE_LAST and sessions >= MIN_SESSIONS_SINCE_LAST


def acquire_lock(memory_dir: str) -> bool:
    """Acquire consolidation lock. Returns True if acquired."""
    path = _lock_path(memory_dir)

    # Check existing lock
    try:
        with open(path) as f:
            holder_pid = int(f.read().strip())
        lock_mtime = os.path.getmtime(path)
        age = time.time() - lock_mtime

        # Lock is fresh and holder is alive
        if age < LOCK_STALE_SECONDS:
            try:
                os.kill(holder_pid, 0)  # Check if process exists
                return False  # Lock held by live process
            except OSError:
                pass  # Dead process, reclaim
    except (OSError, ValueError):
        pass  # No lock file or invalid

    # Write our PID
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        return False

    # Verify we won the race
    try:
        with open(path) as f:
            verify_pid = int(f.read().strip())
        return verify_pid == os.getpid()
    except (OSError, ValueError):
        return False


def release_lock(memory_dir: str) -> None:
    """Release consolidation lock."""
    path = _lock_path(memory_dir)
    try:
        os.remove(path)
    except OSError:
        pass


async def dream_consolidate(memory_dir: str, *, scope: str, model_id: str) -> str:
    """Run dream consolidation over one memory tier. Returns summary of changes."""
    if not should_consolidate(memory_dir):
        return "Consolidation not needed yet (requires 24h + 5 turn-ends since last run)."

    if not acquire_lock(memory_dir):
        return "Consolidation already in progress (locked by another process)."

    try:
        from server.agents.runtime import run_agent

        # Runs under the memory_consolidator agent (consolidation-specific system
        # prompt), not memory_extractor whose extraction rules conflict with a
        # reorganize-only task. The scoped, phased plan is the task body.
        result = await run_agent(
            CONSOLIDATION_PROMPT.format(scope=scope),
            agent_type="memory_consolidator",
            session_id="dream",
            depth=1,
        )

        # Update metadata
        meta = _load_meta(memory_dir)
        meta["last_consolidated_at"] = time.time()
        meta["session_count"] = 0
        _save_meta(memory_dir, meta)

        if result.status == "completed":
            log.info("Dream consolidation (%s) completed: %d turns", scope, result.turns_used)
            return f"Consolidation complete. {result.turns_used} turns used.\n\n{result.output}"
        else:
            log.warning("Dream consolidation %s: %s", result.status, result.output[:200])
            return f"Consolidation {result.status}: {result.output[:500]}"

    except Exception as e:
        log.error("Dream consolidation failed: %s", e, exc_info=True)
        return f"Consolidation failed: {e}"
    finally:
        release_lock(memory_dir)


async def record_and_maybe_dream(ws_path: str | None, *, model_id: str) -> None:
    """Post-turn hook: bump per-tier session counters and consolidate any tier
    that is due. Fire-and-forget; feature-flag gated internally.

    This is what actually TRIGGERS dream consolidation. (The original
    dream_consolidate was never called from anywhere: sessions were recorded,
    thresholds accrued, and the consolidation agent never ran.)
    """
    from server.infrastructure.feature_flags import is_enabled

    if not is_enabled("dream_consolidation"):
        return

    tiers: list[tuple[str, str]] = []
    global_dir = ensure_global_memory_dir()
    if global_dir:
        tiers.append((SCOPE_GLOBAL, global_dir))
    project_dir = ensure_memory_dir(ws_path)
    if project_dir:
        tiers.append((SCOPE_PROJECT, project_dir))

    for scope, memory_dir in tiers:
        try:
            record_session(memory_dir)
            if should_consolidate(memory_dir):
                await dream_consolidate(memory_dir, scope=scope, model_id=model_id)
        except Exception as e:
            log.warning("dream consolidation (%s) skipped: %s", scope, e)
