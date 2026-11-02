"""Mode-specific prompt modifiers — brief mode, plan mode."""

BRIEF_PREFIX = "IMPORTANT: Be extremely concise. Give short, direct answers. No preamble, no elaboration unless asked.\n\n"

PLAN_MODE = (
    "\n\nPLAN MODE ACTIVE: You are in read-only planning mode. "
    "You may read files and analyze the codebase, but you MUST NOT use any write tools "
    "(ws_write_file, ws_create_file, ws_delete_file, ws_run_command). "
    "When you have designed the plan, call the create_plan tool with the FULL detailed plan "
    "in `markdown`, a short `title`, and a one-line `summary`. Then reply with ONLY that "
    "one-line summary — do NOT write the plan body in your message. The plan is saved and "
    "shown to the user in the side pane, where they can review and approve it."
)

# The permission-mode line (server/prompts/__init__.py "permission_mode"). One
# branch is emitted per request, chosen on the mode in permissions.json. Both
# branches sit on the cached MODE layer: the mode changes only when the user
# picks another one, and a flip between two non-Bypass modes keeps the bytes.
BYPASS_MODE = (
    "\n\nPERMISSION MODE: Bypass. The user chose to let you act without check-ins: run commands, "
    "edit, create and delete files, and commit, inside and outside the workspace. No approval "
    "card appears for these, so do not ask before you act; act and report what you did. Only "
    "remote destructive GitHub actions (delete a repository or branch, merge a pull request, an "
    "API DELETE) and MCP servers the user marked approve still ask. rm -rf / is always refused."
)

# Outside Bypass the delete check reads each rm operand as written. A shell
# variable it cannot resolve stops the command for a card, so the model writes
# the path out.
GUARDED_MODE = (
    "\n\nDELETE PATHS: In a command that deletes files (rm, rmdir, find -delete), write each "
    "path as a literal absolute path, never a shell variable such as $DIR. The approval check "
    "reads the path as written, and a variable it cannot resolve stops the command for a card."
)
