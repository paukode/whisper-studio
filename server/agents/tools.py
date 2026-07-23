"""
Agent-specific tool definitions — tools available inside the agent runtime.

These tools are added to the agent's tool pool and handled inline by the runtime.
They are NOT added to the main chat tool pool (except spawn_agent and send_message
which are also exposed at the top level).
"""

SPAWN_AGENT_TOOL = {
    "name": "spawn_agent",
    "description": (
        "Spawn a child agent to handle a subtask. The child agent has full tool access "
        "based on its type. Available types: general (full tools), explore (read-only, fast), "
        "plan (read-only, structured planning), verify (runs tests, returns PASS/FAIL verdict), "
        "coordinator (orchestrates other agents, no file access)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Self-contained brief for the child agent, written for "
                    "someone with no chat context: objective, scope and "
                    "inputs, constraints, and the expected output format "
                    "with acceptance criteria"
                ),
            },
            "agent_type": {
                "type": "string",
                "enum": ["general", "explore", "plan", "verify", "coordinator"],
                "description": "Type of agent to spawn (default: general)",
            },
            "context": {
                "type": "string",
                "description": "Optional context/background for the agent",
            },
        },
        "required": ["task"],
    },
}

SEND_MESSAGE_TOOL = {
    "name": "send_message",
    "description": (
        "Send a message to another agent by ID. Use broadcast=true to send to all agents. "
        "Messages are delivered to the agent's mailbox and read at the start of their next turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to_agent_id": {
                "type": "string",
                "description": "Target agent ID (from list_agents or spawn_agent result)",
            },
            "content": {
                "type": "string",
                "description": "Message content",
            },
            "broadcast": {
                "type": "boolean",
                "description": "If true, send to all agents in the session",
            },
        },
        "required": ["content"],
    },
}

LIST_AGENTS_TOOL = {
    "name": "list_agents",
    "description": "List all active agents in the current session with their IDs, types, and status.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

RECEIVE_MESSAGES_TOOL = {
    "name": "receive_messages",
    "description": "Check and retrieve pending messages from other agents.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

COMPLETE_COORDINATION_TOOL = {
    "name": "complete_coordination",
    "description": (
        "Signal that coordination is complete. Only used by coordinator agents. "
        "Provide a summary of what was accomplished."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Summary of what was accomplished by all agents",
            },
        },
        "required": ["summary"],
    },
}


def get_agent_runtime_tools(agent_id: str, depth: int) -> list[dict]:
    """Get tools available inside the agent runtime.

    These are injected into the agent's tool pool in addition to workspace/skill tools.
    Spawn is only available if depth < 4 (prevents runaway nesting).
    """
    tools = [LIST_AGENTS_TOOL, RECEIVE_MESSAGES_TOOL, SEND_MESSAGE_TOOL]
    if depth < 4:
        tools.append(SPAWN_AGENT_TOOL)
    tools.append(COMPLETE_COORDINATION_TOOL)
    return tools
