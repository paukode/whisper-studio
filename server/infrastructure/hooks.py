"""Deprecated shim — the hooks system now lives in ``server.hooks``.

The old observe-only ``fire_hook`` and the index-addressed flat API were
replaced by the blocking-hooks engine (``server.hooks.engine.run_hooks`` /
``check_stop_hooks``) and the v2 router (``server.hooks.routes``). This module
re-exports the new router so any lingering ``from server.infrastructure.hooks
import router`` keeps working, and keeps ``fire_hook`` as a thin delegating
adapter for out-of-tree callers. New code must import from ``server.hooks``.
"""

from __future__ import annotations

import logging

from server.hooks.routes import router  # noqa: F401 — re-exported for main.py

log = logging.getLogger("whisper-studio")


async def fire_hook(event: str, context: dict | None = None) -> list[str]:
    """Deprecated. Delegates to the new engine and returns any additionalContext
    strings the hooks produced (the old return contract). Prefer
    ``server.hooks.run_hooks`` / ``check_stop_hooks`` directly."""
    from server.hooks import run_hooks
    from server.hooks.schema import canonical_event

    ctx = context or {}
    payload = {"event": canonical_event(event), **ctx}
    outcome = await run_hooks(
        canonical_event(event),
        payload,
        tool_name=ctx.get("tool_name"),
        allow_rewrite=False,
    )
    return [f"[Hook:{event}] {c}" for c in outcome.contexts]
