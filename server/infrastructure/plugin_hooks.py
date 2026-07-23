"""
In-process hook registry.

Plugins (and the workflow runtime, WS-E) register async interceptors keyed by
event. They run in-process — no shell, no sandbox — and can block or add
context. This is the single Python-side registration point the hooks engine
(server/hooks/engine.py) consumes as its first evaluation phase.

``register_hook(event, fn)`` where ``async fn(payload) -> dict | None`` returns
the structured-control dict ({decision, reason, updatedInput, additionalContext})
or None to pass. ``register_pre_tool_hook`` is kept as a thin adapter so
plugins/security_checks.py is untouched.
"""

import logging

log = logging.getLogger("whisper-studio")

# event -> list of async callables
_hooks: dict[str, list] = {}


def register_hook(event: str, fn) -> None:
    """Register an in-process hook: async fn(payload: dict) -> dict | None."""
    _hooks.setdefault(event, []).append(fn)
    log.info("In-process hook registered for %s: %s", event, getattr(fn, "__qualname__", fn))


def unregister_hook(event: str, fn) -> None:
    if event in _hooks and fn in _hooks[event]:
        _hooks[event].remove(fn)


async def run_inprocess(event: str, payload: dict) -> list[dict]:
    """Run all in-process hooks for an event; returns their non-None results
    (structured-control dicts) in registration order. The engine merges them."""
    results: list[dict] = []
    for hook in list(_hooks.get(event, ())):
        try:
            r = await hook(payload)
            if r is not None:
                results.append(r)
        except Exception as e:
            log.warning("In-process hook error (%s): %s", event, e)
    return results


# ── Back-compat adapter (plugins/security_checks.py) ─────────────────────────


def register_pre_tool_hook(fn):
    """Adapter: an old-style ``async fn(tool_name, tool_input) -> dict|None``
    (dict = block with {"reason", "findings"}) registered as a PreToolUse
    in-process hook that emits the structured-control shape."""

    async def _adapter(payload: dict):
        block = await fn(payload.get("tool_name", ""), payload.get("tool_input", {}))
        if block is None:
            return None
        return {
            "decision": "deny",
            "reason": block.get("reason", "Blocked by plugin hook."),
            "_findings": block.get("findings"),
        }

    register_hook("PreToolUse", _adapter)
    log.info("Plugin pre-tool hook registered: %s", getattr(fn, "__qualname__", fn))


async def run_pre_tool_hooks(tool_name: str, tool_input: dict) -> dict | None:
    """Legacy entry point kept for any direct caller. Prefer the engine."""
    results = await run_inprocess("PreToolUse", {"tool_name": tool_name, "tool_input": tool_input})
    for r in results:
        if r.get("decision") == "deny":
            return {"reason": r.get("reason", ""), "findings": r.get("_findings")}
    return None
