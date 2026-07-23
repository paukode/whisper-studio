"""
Inter-agent messaging — in-memory message bus for agent communication.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class AgentMessage:
    """A message between agents."""

    from_id: str
    to_id: str  # "*" for broadcast
    content: str
    timestamp: float = field(default_factory=time.time)


class MessageBus:
    """Thread-safe message bus for inter-agent communication."""

    def __init__(self):
        self._mailboxes: dict[str, list[AgentMessage]] = {}
        # agent_id -> owning chat session ("" = untagged/legacy)
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def send(self, from_id: str, to_id: str, content: str) -> None:
        """Send a message to a specific agent."""
        msg = AgentMessage(from_id=from_id, to_id=to_id, content=content)
        with self._lock:
            if to_id not in self._mailboxes:
                self._mailboxes[to_id] = []
            self._mailboxes[to_id].append(msg)

    def broadcast(self, from_id: str, content: str, agent_ids: list[str] | None = None) -> int:
        """Broadcast a message to multiple agents.

        If agent_ids is None, sends to all mailboxes IN THE SENDER'S SESSION
        (or, for untagged senders like "main", only untagged/same-session
        mailboxes) — a broadcast from one session must never bleed into
        another session's agents.
        """
        msg_template = AgentMessage(from_id=from_id, to_id="*", content=content)
        count = 0
        with self._lock:
            if agent_ids:
                targets = agent_ids
            else:
                sender_session = self._sessions.get(from_id, "")
                targets = [
                    aid for aid in self._mailboxes if self._sessions.get(aid, "") == sender_session
                ]
            for aid in targets:
                if aid == from_id:
                    continue
                if aid not in self._mailboxes:
                    self._mailboxes[aid] = []
                self._mailboxes[aid].append(
                    AgentMessage(
                        from_id=from_id,
                        to_id=aid,
                        content=content,
                        timestamp=msg_template.timestamp,
                    )
                )
                count += 1
        return count

    def receive(self, agent_id: str, clear: bool = True) -> list[AgentMessage]:
        """Receive all pending messages for an agent.

        If clear=True, messages are removed from the mailbox after reading.
        """
        with self._lock:
            messages = self._mailboxes.get(agent_id, [])
            if clear:
                self._mailboxes[agent_id] = []
            return list(messages)

    def peek(self, agent_id: str) -> int:
        """Return count of pending messages without consuming them."""
        with self._lock:
            return len(self._mailboxes.get(agent_id, []))

    def create_mailbox(self, agent_id: str, session_id: str = "") -> None:
        with self._lock:
            if agent_id not in self._mailboxes:
                self._mailboxes[agent_id] = []
            if session_id:
                self._sessions[agent_id] = session_id

    def delete_mailbox(self, agent_id: str) -> None:
        with self._lock:
            self._mailboxes.pop(agent_id, None)
            self._sessions.pop(agent_id, None)


# Singleton
message_bus = MessageBus()
