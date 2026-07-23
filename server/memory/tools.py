"""Memory tool schemas — Bedrock tool definitions for memory operations.

These tools allow the LLM to read, write, list, and delete memory files
across the two-tier store: global (data/global_memory/, cross-workspace,
works without a workspace) and project (data/memory/<slug>/, needs an
open workspace).
"""

_SCOPE_PROPERTY = {
    "type": "string",
    "enum": ["global", "project"],
    "description": (
        "Memory tier. 'global' persists across every workspace and plain chat; "
        "'project' is scoped to the open workspace."
    ),
}

MEMORY_TOOLS: list[dict] = [
    {
        "name": "memory_read",
        "description": (
            "Read a memory file from the memory store. "
            "Use this to recall previously stored information. "
            "Without a scope, searches project memory first, then global."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Relative path within the memory directory (e.g. 'user_role.md')",
                },
                "scope": _SCOPE_PROPERTY,
            },
            "required": ["filename"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Write a memory file to the memory store. Creates or overwrites a file "
            "with YAML frontmatter (name, description, type) and markdown content. "
            "Scope routing: cross-project facts about the user (type user/feedback) "
            "belong in global; repo-specific facts (type project/reference) belong in "
            "project. Without an explicit scope, a file that already exists in some "
            "tier is updated in place; new files route by type (project falls back "
            "to global when no workspace is open). "
            "Types: user (preferences/role), feedback (guidance on approach), "
            "project (goals/deadlines/context), reference (external system pointers)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Relative path within memory directory (e.g. 'feedback_testing.md')",
                },
                "name": {
                    "type": "string",
                    "description": "Memory name for the frontmatter",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description used for relevance filtering in future sessions",
                },
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": "Memory type category",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown content body (without frontmatter, it is added automatically)",
                },
                "scope": _SCOPE_PROPERTY,
            },
            "required": ["filename", "name", "description", "type", "content"],
        },
    },
    {
        "name": "memory_list",
        "description": (
            "List all memory files in both tiers (global and project) with their "
            "type, description, and modification date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "Delete a memory file from the memory store. "
            "Without a scope, searches project memory first, then global."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Relative path within memory directory to delete",
                },
                "scope": _SCOPE_PROPERTY,
            },
            "required": ["filename"],
        },
    },
]

MEMORY_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in MEMORY_TOOLS)
