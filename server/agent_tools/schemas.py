"""Tool schemas surfaced to the model, plus the aggregate ``AGENT_TOOLS`` list.

Pure data — no imports, no side effects. The executors that back these live in
sibling modules (config_tools, skill_tools, mcp_tools, search_tools, spawn,
teams) and are dispatched by server/tool_router.py.
"""

# ── Config tools ──────────────────────────────────────────────────────────────

CONFIG_GET_TOOL = {
    "name": "config_get",
    "description": "Read one or more Whisper Studio config values (e.g. model, region, brief_mode). Call with no keys to list all.",
    "input_schema": {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Config keys to read. Empty = return all.",
            },
        },
        "required": [],
    },
}

CONFIG_SET_TOOL = {
    "name": "config_set",
    "description": (
        "Write Whisper Studio config values. Only known keys are accepted: "
        "whisper_language, bedrock_region, default_chat_model, brief_mode, permission_mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "updates": {
                "type": "object",
                "description": "Key-value pairs to update in config",
            },
        },
        "required": ["updates"],
    },
}

# ── Skill invoke tool ─────────────────────────────────────────────────────────

SKILL_INVOKE_TOOL = {
    "name": "skill_invoke",
    "description": (
        "Invoke a named Whisper skill by name. Use this to delegate to a specialized skill "
        "mid-conversation (e.g. invoke 'web_search' or 'summarize_transcript'). "
        "Call skill_list first to see available skills."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Name of the skill to invoke",
            },
            "input": {
                "type": "string",
                "description": "Input/question to pass to the skill",
            },
        },
        "required": ["skill_name", "input"],
    },
}

SKILL_LIST_TOOL = {
    "name": "skill_list",
    "description": "List all available Whisper skills with their names and descriptions.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

# ── Notify user (BriefTool equivalent) ───────────────────────────────────────

NOTIFY_USER_TOOL = {
    "name": "notify_user",
    "description": (
        "Send a proactive formatted message card to the user. Use this when you want to "
        "surface something important the user hasn't asked for: task completion, a blocker "
        "you hit, a key finding, or a progress update while working in the background. "
        "Supports markdown formatting."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message content. Supports markdown.",
            },
            "status": {
                "type": "string",
                "enum": ["normal", "success", "warning", "error"],
                "description": "Visual style: normal (default), success (green), warning (amber), error (red)",
            },
            "title": {
                "type": "string",
                "description": "Optional short title for the notification card",
            },
        },
        "required": ["message"],
    },
}

# ── MCP resource tools ────────────────────────────────────────────────────────

LIST_MCP_RESOURCES_TOOL = {
    "name": "list_mcp_resources",
    "description": (
        "List available resources from all connected MCP servers. "
        "Resources are data endpoints you can read with read_mcp_resource."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Optional: filter by MCP server name",
            },
        },
        "required": [],
    },
}

READ_MCP_RESOURCE_TOOL = {
    "name": "read_mcp_resource",
    "description": "Read the content of a specific MCP resource by server name and URI.",
    "input_schema": {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "MCP server name",
            },
            "uri": {
                "type": "string",
                "description": "Resource URI (from list_mcp_resources)",
            },
        },
        "required": ["server", "uri"],
    },
}

# ── ToolSearch tool ───────────────────────────────────────────────────────────

TOOL_SEARCH_TOOL = {
    "name": "tool_search",
    "description": (
        "Search the FULL tool catalog, including deferred tools that are "
        "listed under 'Additional tools (not loaded)' in your instructions "
        "but not currently callable. Matches return their full schemas and "
        "are ACTIVATED for this session — callable from your next round. "
        "Use 'select:name1,name2' to load exact tools by name, or plain "
        "keywords to discover by capability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keywords to search for (e.g. 'notebook', 'cron schedule', "
                    "'browser preview'), or 'select:tool_a,tool_b' for exact names"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default 8)",
            },
            "activate": {
                "type": "boolean",
                "description": "Register matches as callable for this session (default true)",
            },
        },
        "required": ["query"],
    },
}

# ── Agent tools (spawn, messaging, teams) ────────────────────────────────────

SPAWN_AGENT_TOOL = {
    "name": "spawn_agent",
    "description": (
        "Spawn an independent agent to handle a subtask with full tool access. "
        "The agent runs a complete tool loop and returns results. Agent types:\n"
        "- general: Full tool access, good for implementation tasks\n"
        "- explore: Read-only, uses fast model — best for code search and analysis\n"
        "- plan: Read-only — produces structured implementation plans\n"
        "- verify: Runs tests and checks, returns PASS/FAIL/PARTIAL verdict\n"
        "- coordinator: Orchestrates other agents, no direct file access"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Self-contained brief for the agent, written for someone "
                    "with no chat context: objective (restated from the user's "
                    "request), scope and inputs, constraints, and the expected "
                    "output format with acceptance criteria"
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
            "detach": {
                "type": "boolean",
                "description": (
                    "Run in the background instead of blocking this turn: "
                    "returns a task_id immediately, a completion update is "
                    "injected into a later turn, and you can poll with "
                    "task_status/task_output. Detached agents run READ-ONLY "
                    "unless isolation='worktree' gives them their own copy "
                    "to write into. Default false."
                ),
            },
            "isolation": {
                "type": "string",
                "enum": ["none", "worktree"],
                "description": (
                    "'worktree' gives the agent its own git worktree so "
                    "parallel agents can write without colliding (and lets a "
                    "DETACHED agent write safely). When the agent completes, "
                    "its changes are applied UNCOMMITTED back to the "
                    "originating branch's working tree (visible in git "
                    "status) and the worktree is removed; on conflict or a "
                    "failed run the worktree is kept and the agent's output "
                    "says where. Default none."
                ),
            },
        },
        "required": ["task"],
    },
}

SEND_MESSAGE_TOOL = {
    "name": "send_message",
    "description": (
        "Send a message to another agent by ID, or broadcast to all agents. "
        "Use list_agents to find agent IDs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to_agent_id": {
                "type": "string",
                "description": "Target agent ID",
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

TEAM_CREATE_TOOL = {
    "name": "team_create",
    "description": (
        "Create a named team of parallel agents for complex multi-part tasks. "
        "Each agent runs with full tool access based on its type. "
        "All agents execute in parallel and results are collected. "
        "Write each agent's task as a self-contained brief: the agent sees "
        "NONE of this conversation, so restate everything it needs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_name": {
                "type": "string",
                "description": "Short name for this team (e.g. 'website-build')",
            },
            "description": {
                "type": "string",
                "description": (
                    "What this team is working on, restating the user's goal "
                    "in one or two sentences"
                ),
            },
            "agents": {
                "type": "array",
                "description": "List of agents to spawn with their individual tasks",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Agent name"},
                        "task": {
                            "type": "string",
                            "description": (
                                "Self-contained brief for this agent, written for "
                                "someone with no chat context: the objective "
                                "(restated from the user's request), scope and "
                                "inputs, constraints, and the expected output "
                                "format with acceptance criteria"
                            ),
                        },
                        "agent_type": {
                            "type": "string",
                            "enum": ["general", "explore", "plan", "verify"],
                            "description": "Agent type (default: general)",
                        },
                    },
                    "required": ["name", "task"],
                },
            },
            "session_id": {"type": "string", "description": "Current session ID"},
        },
        "required": ["team_name", "agents", "session_id"],
    },
}

TEAM_DELETE_TOOL = {
    "name": "team_delete",
    "description": "Disband a team and clean up resources.",
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Team ID to disband"},
        },
        "required": ["team_id"],
    },
}

# ── All agent tools ───────────────────────────────────────────────────────────

AGENT_TOOLS = [
    CONFIG_GET_TOOL,
    CONFIG_SET_TOOL,
    SKILL_INVOKE_TOOL,
    SKILL_LIST_TOOL,
    NOTIFY_USER_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
    TOOL_SEARCH_TOOL,
    SPAWN_AGENT_TOOL,
    SEND_MESSAGE_TOOL,
    LIST_AGENTS_TOOL,
    TEAM_CREATE_TOOL,
    TEAM_DELETE_TOOL,
]
AGENT_TOOL_NAMES = {t["name"] for t in AGENT_TOOLS}
