"""
Agent Intelligence — multi-level hierarchies, typed agents, messaging, and coordination.

Public API:
    AgentConfig, AGENT_TYPES  — agent type definitions
    AgentRegistry             — active agent tracking
    MessageBus                — inter-agent messaging
    run_agent()               — core agent runtime loop with tool execution
"""

from server.agents.config import AGENT_TYPES, AgentConfig
from server.agents.messaging import MessageBus, message_bus
from server.agents.registry import AgentRegistry, agent_registry
from server.agents.runtime import AgentResult, run_agent

__all__ = [
    "AgentConfig",
    "AGENT_TYPES",
    "AgentRegistry",
    "agent_registry",
    "MessageBus",
    "message_bus",
    "run_agent",
    "AgentResult",
]
