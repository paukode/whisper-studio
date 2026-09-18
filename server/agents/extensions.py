"""Live budget extensions for running agents.

An agent's turn cap and deadline are fixed when it starts. Extend lets the
user (through POST /api/agents/{id}/extend) grant a running agent more
rounds or seconds while its context is still hot, instead of letting it
stop at the limit and resuming it later with a reloaded context. The runner
reads the extension dict at every round start (TurnContext.budget_extension),
so a grant applies to the very next round.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_live: dict[str, dict] = {}


def new_extension() -> dict:
    return {"rounds": 0, "seconds": 0.0}


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
        return dict(ext)


def live_ids() -> list[str]:
    with _lock:
        return list(_live)
