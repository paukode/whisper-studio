"""
Git tool definitions — schemas for all git tools exposed to the AI assistant.

Tool schemas follow Whisper's existing tool_pool pattern: each tool is a dict
with name, description, and input_schema. Executor functions are registered
separately in executor.py via @register_executor.
"""

GIT_READ_TOOLS = [
    {
        "name": "git_status",
        "description": (
            "[Git] Show the working tree status. Returns staged, unstaged, "
            "and untracked files with their status codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short": {
                    "type": "boolean",
                    "description": "Use short format output. Default false.",
                },
            },
        },
    },
    {
        "name": "git_diff",
        "description": (
            "[Git] Show file diffs. By default compares working tree to HEAD. "
            "Use 'cached' for staged changes, 'file' for a single file diff "
            "against the merge base (PR-like view)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cached": {
                    "type": "boolean",
                    "description": "Show only staged (cached) changes. Default false.",
                },
                "stat": {
                    "type": "boolean",
                    "description": "Show diffstat summary only (no patch). Default false.",
                },
                "file": {
                    "type": "string",
                    "description": "Show diff for a single file (relative path). Uses merge-base for PR-like diff.",
                },
            },
        },
    },
    {
        "name": "git_log",
        "description": (
            "[Git] Show commit history. Returns commit SHA, author, date, "
            "and message for recent commits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max number of commits to show. Default 20.",
                },
                "branch": {
                    "type": "string",
                    "description": "Show commits on a specific branch.",
                },
                "file": {
                    "type": "string",
                    "description": "Show commits affecting a specific file (relative path).",
                },
                "oneline": {
                    "type": "boolean",
                    "description": "Use short one-line format. Default true.",
                },
            },
        },
    },
    {
        "name": "git_branch_list",
        "description": (
            "[Git] List branches. Shows current branch indicator. "
            "Use 'all' to include remote-tracking branches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "Include remote-tracking branches. Default false.",
                },
                "remote": {
                    "type": "boolean",
                    "description": "Show only remote-tracking branches. Default false.",
                },
            },
        },
    },
    {
        "name": "git_show",
        "description": (
            "[Git] Show details of a commit, tag, or other git object. "
            "Includes commit message, author, date, and diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Commit SHA, branch name, tag, or other ref. Default HEAD.",
                },
                "stat": {
                    "type": "boolean",
                    "description": "Show diffstat only (no full diff). Default false.",
                },
            },
        },
    },
    {
        "name": "git_blame",
        "description": (
            "[Git] Show line-by-line annotation of a file, indicating "
            "which commit last modified each line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Relative path of the file to blame.",
                },
                "line_start": {
                    "type": "integer",
                    "description": "Start line number for partial blame.",
                },
                "line_end": {
                    "type": "integer",
                    "description": "End line number for partial blame.",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "git_stash_list",
        "description": "[Git] List all stash entries with their index and description.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

GIT_WRITE_TOOLS = [
    {
        "name": "git_clone",
        "description": (
            "[Git] Clone a remote repository and (by default) open it as the workspace. "
            "This is the ONLY git tool that does NOT require a connected workspace — "
            "use it when the user says 'clone X', 'pull this repo', 'open https://github.com/...', "
            "or 'set up <repo>'. Refuses overwriting an existing non-empty directory. "
            "Fails fast on missing credentials (no hanging on prompts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Remote repository URL. Accepts https://, ssh://, or git@host:path form.",
                },
                "dest": {
                    "type": "string",
                    "description": "Local destination directory. Optional — defaults to ~/Documents/<repo-name>. Must not exist or must be empty.",
                },
                "open": {
                    "type": "boolean",
                    "description": "After cloning, connect the cloned directory as the workspace. Default true.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "git_add_commit",
        "description": (
            "[Git] Stage files and create a git commit. Specify files to stage "
            "or set 'all' to stage all tracked changes. Never amends unless "
            "explicitly requested. Rejects commits containing secret files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage (relative paths). If empty and 'all' is true, stages all changes.",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Stage all tracked file changes. Default false.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_push",
        "description": (
            "[Git] Push commits to the remote repository. Sets upstream tracking on first push."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch to push. Defaults to current branch.",
                },
                "set_upstream": {
                    "type": "boolean",
                    "description": "Set upstream tracking (-u flag). Default true for new branches.",
                },
            },
        },
    },
    {
        "name": "git_create_branch",
        "description": (
            "[Git] Create a new branch and switch to it. Optionally specify a starting point."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the new branch.",
                },
                "from_ref": {
                    "type": "string",
                    "description": "Starting point (commit, branch, tag). Default HEAD.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "git_push_pr",
        "description": (
            "[Git] Push current branch and create a GitHub pull request. "
            "Requires the 'gh' CLI to be installed and authenticated. "
            "Will suggest creating a new branch if on the default branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PR title (short, under 70 chars).",
                },
                "body": {
                    "type": "string",
                    "description": "PR body/description in markdown.",
                },
                "base": {
                    "type": "string",
                    "description": "Base branch for the PR. Defaults to repo default branch.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "git_checkout",
        "description": (
            "[Git] Switch to an existing branch or ref. "
            "Use git_create_branch to create a new branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name or ref to switch to.",
                },
            },
            "required": ["branch"],
        },
    },
    {
        "name": "git_merge",
        "description": ("[Git] Merge a branch into the current branch."),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch to merge into current branch.",
                },
                "no_ff": {
                    "type": "boolean",
                    "description": "Create a merge commit even for fast-forward merges. Default false.",
                },
            },
            "required": ["branch"],
        },
    },
    {
        "name": "git_stash",
        "description": (
            "[Git] Stash or restore working directory changes. "
            "Actions: push (save changes), pop (apply and remove top stash), "
            "apply (apply without removing), drop (discard a stash entry)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["push", "pop", "apply", "drop"],
                    "description": "Stash action to perform.",
                },
                "message": {
                    "type": "string",
                    "description": "Message for stash push. Optional.",
                },
                "index": {
                    "type": "integer",
                    "description": "Stash index for pop/apply/drop. Default 0 (top).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "git_delete_branch",
        "description": (
            "[Git] Delete a local branch. "
            "Must be on a different branch before deleting. "
            "Use force=true to delete unmerged branches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name to delete.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force delete even if unmerged (-D flag). Default false.",
                },
            },
            "required": ["branch"],
        },
    },
]

GIT_TOOLS = GIT_READ_TOOLS + GIT_WRITE_TOOLS


def get_git_tools() -> list[dict]:
    """Return all git tool definitions for the tool pool."""
    return GIT_TOOLS
