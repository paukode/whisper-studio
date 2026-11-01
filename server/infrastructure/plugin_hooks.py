"""
In-process hook registry.

Plugins register async interceptors keyed by event. They run in-process (no
shell, no sandbox) and can block or add context. This is the single Python-side registration point the hooks engine
(server/hooks/engine.py) consumes as its first evaluation phase.

``register_hook(event, fn)`` where ``async fn(payload) -> dict | None`` returns
the structured-control dict ({decision, reason, updatedInput, additionalContext})
or None to pass. ``register_pre_tool_hook`` is kept as a thin adapter so
plugins/security_checks.py is untouched.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger("whisper-studio")


@dataclass(frozen=True)
class InProcessHookError:
    """A crashed in-process hook on a non-PreToolUse event. A separate type,
    not a key in a control dict, so JSON a shell hook prints can never pose
    as one."""

    message: str


# event -> list of async callables
_hooks: dict[str, list] = {}


def register_hook(event: str, fn) -> None:
    """Register an in-process hook: async fn(payload: dict) -> dict | None."""
    _hooks.setdefault(event, []).append(fn)
    log.info("In-process hook registered for %s: %s", event, getattr(fn, "__qualname__", fn))


async def run_inprocess(event: str, payload: dict) -> list[dict | InProcessHookError]:
    """Run all in-process hooks for an event; returns their non-None results
    (structured-control dicts) in registration order. The engine merges them.

    A hook that raises fails closed on PreToolUse (a deny naming the hook and
    its error, so a crashed security check never lets the call through) and
    is reported as an InProcessHookError on every other event."""
    results: list[dict | InProcessHookError] = []
    for hook in list(_hooks.get(event, ())):
        try:
            r = await hook(payload)
        except Exception as e:
            name = getattr(hook, "__qualname__", repr(hook))
            log.exception("In-process hook %s failed on %s", name, event)
            if event == "PreToolUse":
                results.append({"decision": "deny", "reason": f"Hook {name} failed: {e}"})
            else:
                results.append(InProcessHookError(f"hook {name}: {e}"))
            continue
        if r is not None:
            results.append(r)
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
