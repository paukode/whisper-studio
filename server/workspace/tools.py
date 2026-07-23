"""Tool schemas advertised to the LLM.

Three schema groups:
- get_global_workspace_tools(): always-available tools (currently just
  ws_open_folder so the model can connect a workspace mid-conversation).
- get_workspace_tools(): tools that require a connected workspace
  (read/list/grep/glob + write/create/edit/delete + run_command).
- get_worktree_tools(): only surfaced when the workspace is a git repo.
"""

import os

from .state import get_workspace_path


def get_global_workspace_tools() -> list[dict]:
    """Tools always available regardless of whether a workspace is connected."""
    return [
        {
            "name": "ws_open_folder",
            "description": (
                "[Workspace] Connect a specific folder as the workspace. "
                "ONLY use this when NO workspace is currently connected and the user explicitly asks to open or create a project folder. "
                "Do NOT call this if a workspace is already connected — use ws_read_file/ws_write_file/ws_create_file instead. "
                "Supports ~ expansion and absolute paths."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path or ~/relative path for the folder (e.g. '~/Desktop/my-website')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional short description of what this folder is for",
                    },
                },
                "required": ["path"],
            },
        },
    ]


def get_workspace_tools() -> list[dict]:
    ws = get_workspace_path()
    if not ws:
        return []
    read_tools = [
        {
            "name": "ws_read_file",
            "description": "[Workspace] Read a file from the connected codebase. Returns the COMPLETE file content with line numbers. Always reads the entire file in one call — do NOT use offset/limit unless the file is very large (2000+ lines).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the workspace (e.g. 'src/main.py')",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start line (1-based). ONLY use for files over 2000 lines.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines. ONLY use for files over 2000 lines.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "ws_list_directory",
            "description": "[Workspace] List files and directories in a workspace directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path. Use '' or '.' for workspace root.",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "ws_grep",
            "description": (
                "[Workspace] Search file contents with regex powered by ripgrep. "
                "Default mode (files_with_matches) returns only file paths for minimal token usage. "
                "Use output_mode='content' for matching lines, 'count' for per-file match counts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "Output format. files_with_matches (default): file paths only. content: matching lines. count: per-file counts.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "File glob filter. Supports comma-separated (*.js,*.ts), brace-expanded (*.{ts,tsx}), or space-separated patterns.",
                    },
                    "type": {
                        "type": "string",
                        "description": "File type filter using ripgrep types, e.g. 'py', 'js', 'rust'. More efficient than glob for standard types.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive search. Default false (case-insensitive).",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context before and after each match (content mode only). Supersedes context_before/context_after.",
                    },
                    "context_before": {
                        "type": "integer",
                        "description": "Lines before each match (content mode only).",
                    },
                    "context_after": {
                        "type": "integer",
                        "description": "Lines after each match (content mode only).",
                    },
                    "show_line_numbers": {
                        "type": "boolean",
                        "description": "Show line numbers in content mode. Default true.",
                    },
                    "max_columns": {
                        "type": "integer",
                        "description": "Max characters per line in content mode (default 600). Set 0 to disable.",
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": "Enable multiline mode where . matches newlines and patterns can span lines. Default false.",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "Max results to return (default 200). Set 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results for pagination. Default 0.",
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "ws_glob",
            "description": (
                "[Workspace] Find files by glob pattern. Returns file paths sorted by modification time (most recent first)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or 'src/**/test_*.js'",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "Max files to return (default 500). Set 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results for pagination. Default 0.",
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "workspace_semantic_search",
            "description": (
                "[Workspace] Semantic search over the indexed workspace by meaning, not keywords. "
                "Covers ALL indexed files — code, PDFs, Word docs, spreadsheets, and scanned/"
                "image documents (OCR'd at index time) — so it sees content ws_grep cannot. "
                "Embeds your natural-language query and returns the most relevant passages "
                "(file paths + line ranges + snippets) plus passages linked through shared "
                "entities (a knowledge-graph hop). PREFER THIS for any question about what the "
                "workspace's files or documents say, or when you don't know the exact term to "
                "grep for. ALWAYS cite the source files it returns, copying the markdown "
                "[path:lines](#wsfile=...&L=start-end) links from the results exactly so the "
                "user can open them at the cited lines in the side panel, and end your answer "
                "with a 'Sources' section listing those links. "
                "Requires the workspace to be indexed; returns a notice if it isn't."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of what you're looking for, e.g. 'where are uploads cancelled' or 'retry/backoff logic'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max matches to return (default 8, max 25).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "workspace_graph_query",
            "description": (
                "[Workspace] Look up TYPED RELATIONSHIPS about a specific entity (person, "
                "organization, project, product…) in the indexed knowledge graph: who they "
                "work with, what they own or contributed to, where they are located, and so "
                "on, aggregated across all indexed files with a confidence and source "
                "citation for each fact. PREFER THIS over semantic search for direct "
                "relationship questions ('who works at X', 'what does Y depend on', 'who are "
                "the parties to Z'). Cite the [path:lines](#wsfile=...&L=start-end) links it "
                "returns exactly and end with a 'Sources' section. Requires the workspace to "
                "be indexed with typed relations enabled; returns a notice otherwise."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "The entity to look up, e.g. a person or organization name.",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Optional: restrict to one relationship type, e.g. works_at, owns, located_in.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max facts to return (default 15, max 40).",
                    },
                },
                "required": ["entity"],
            },
        },
    ]
    write_tools = [
        {
            "name": "ws_write_file",
            "description": "[Workspace] Modify an existing file. Provide the full new content. Requires user approval before writing.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to modify",
                    },
                    "content": {"type": "string", "description": "The complete new file content"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "ws_create_file",
            "description": "[Workspace] Create a new file. Requires user approval.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path for the new file"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "ws_edit_file",
            "description": (
                "[Workspace] Edit an existing file by replacing a specific string. "
                "More efficient than ws_write_file for small changes — sends only the diff. "
                "The old_string must match EXACTLY (including indentation). "
                "The old_string must be unique in the file. "
                "Set replace_all=true to replace every occurrence. "
                "Handles typographic quote normalization automatically."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the file to edit"},
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find and replace. Must be unique in the file unless replace_all is true.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement text. Must differ from old_string.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences of old_string. Default false (replace first unique match only).",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
        {
            "name": "ws_delete_file",
            "description": "[Workspace] Delete a file. Requires user approval.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the file to delete"}
                },
                "required": ["path"],
            },
        },
        {
            "name": "ws_run_command",
            "description": (
                "[Workspace] Run a shell command in the workspace directory. "
                "Requires user approval for write commands. Read-only commands run directly. "
                "Commands that take longer than 30s keep running as a background task (the "
                "same process is handed off, not restarted). Set run_in_background=true to "
                "start in background immediately for known long-running commands. Check "
                "background tasks with task_status and read their output with task_output; "
                "a task event also lands in the session when one finishes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Start the command in background immediately. Use for commands you know will take a long time (builds, large tests). Default false.",
                    },
                },
                "required": ["command"],
            },
        },
    ]
    return read_tools + write_tools


def get_worktree_tools() -> list[dict]:
    """Feature 8: Return worktree tools when workspace is a git repo."""
    ws = get_workspace_path()
    if not ws or not os.path.isdir(os.path.join(ws, ".git")):
        return []
    return [
        {
            "name": "ws_create_worktree",
            "description": (
                "[Workspace] Create an isolated git worktree branch for multi-file changes. "
                "Changes are sandboxed until merged. Returns the worktree name and branch."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Worktree name (e.g. 'feature-auth'). Auto-generated if omitted.",
                    },
                },
            },
        },
        {
            "name": "ws_diff_worktree",
            "description": "[Workspace] Show the diff between the worktree branch and the main branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Worktree name"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "ws_merge_worktree",
            "description": "[Workspace] Merge a worktree branch back into the current branch. Requires user approval.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Worktree name to merge"},
                },
                "required": ["name"],
            },
        },
    ]
