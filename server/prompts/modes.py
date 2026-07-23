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
