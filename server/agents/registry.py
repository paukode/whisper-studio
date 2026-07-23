"""
Agent registry — tracks active agents, their hierarchy, and status.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class AgentInfo:
    """Tracked state for a running agent."""

    agent_id: str
    agent_type: str
    parent_id: str | None
    task: str
    status: str = "running"  # running | completed | failed | stopped
    result: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    model: str | None = None
    session_id: str = ""


class AgentRegistry:
    """Thread-safe registry of active agents."""

    def __init__(self):
        self._agents: dict[str, AgentInfo] = {}
        self._lock = threading.Lock()

    def register(
        self,
        agent_id: str,
        agent_type: str,
        task: str,
        *,
        parent_id: str | None = None,
        model: str | None = None,
        session_id: str = "",
    ) -> AgentInfo:
        info = AgentInfo(
            agent_id=agent_id,
            agent_type=agent_type,
            parent_id=parent_id,
            task=task,
            model=model,
            session_id=session_id,
        )
        with self._lock:
            self._agents[agent_id] = info
        return info

    def update_status(self, agent_id: str, status: str, result: str = "") -> None:
        with self._lock:
            info = self._agents.get(agent_id)
            if info:
                info.status = status
                info.result = result
                if status in ("completed", "failed", "stopped"):
                    info.completed_at = time.time()

    def get(self, agent_id: str) -> AgentInfo | None:
        with self._lock:
            return self._agents.get(agent_id)

    def get_children(self, parent_id: str) -> list[AgentInfo]:
        with self._lock:
            return [a for a in self._agents.values() if a.parent_id == parent_id]

    def list_all(self, session_id: str = "") -> list[AgentInfo]:
        with self._lock:
            agents = list(self._agents.values())
        if session_id:
            agents = [a for a in agents if a.session_id == session_id]
        return agents

    def remove(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """Remove completed agents older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        removed = 0
        with self._lock:
            to_remove = [
                aid
                for aid, info in self._agents.items()
                if info.status in ("completed", "failed", "stopped")
                and info.completed_at is not None
                and info.completed_at < cutoff
            ]
            for aid in to_remove:
                del self._agents[aid]
                removed += 1
        return removed


# Singleton
agent_registry = AgentRegistry()
