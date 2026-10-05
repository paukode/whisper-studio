"""Live budget extensions for running agents.

An agent's turn cap and deadline are fixed when it starts. Extend lets the
user (through POST /api/agents/{id}/extend) grant a running agent more
rounds or seconds while its context is still hot, instead of letting it
stop at the limit and resuming it later with a reloaded context. The runner
reads the extension dict at every round start (TurnContext.budget_extension),
so a grant applies to the very next round. The same dict can also end a run
early: a true "finish" makes the next round the last one, tools off, so the
agent answers with what it has (set by ToolScope.refused after repeated
refused calls, server/agents/tool_access.py). "waited" is the time the run
spent waiting on agents it started (ToolScope.waited): each of those runs on
its own limits, so the runner moves the deadline by it and the card's time
bar shows only the run's own time.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_live: dict[str, dict] = {}


def new_extension() -> dict:
    return {"rounds": 0, "seconds": 0.0, "waited": 0.0}


def register(agent_id: str, extension: dict) -> None:
    with _lock:
        _live[agent_id] = extension


def unregister(agent_id: str) -> None:
    with _lock:
        _live.pop(agent_id, None)


def get(agent_id: str) -> dict | None:
    with _lock:
        ext = _live.get(agent_id)
        return dict(ext) if ext is not None else None


def extend(agent_id: str, *, rounds: int = 0, seconds: float = 0.0) -> dict | None:
    """Add rounds and seconds to a running agent's budget. None when no such
    agent is running (finished agents are resumed, not extended)."""
    with _lock:
        ext = _live.get(agent_id)
        if ext is None:
            return None
        ext["rounds"] = int(ext.get("rounds") or 0) + max(0, int(rounds or 0))
        ext["seconds"] = float(ext.get("seconds") or 0.0) + max(0.0, float(seconds or 0.0))
        if rounds and ext.get("finish") and "finish_at" not in ext:
            # A run told to finish after refused calls goes on with the grant,
            # unless its final round has already started without tools (the
            # runner pinned finish_at); one more refused call ends it again.
            ext.pop("finish")
        return dict(ext)


def live_ids() -> list[str]:
    with _lock:
        return list(_live)
