"""Prompt rendering shared by the prompt tools.

The tool result is a model PROMPT (the user's arguments followed by the
skill instructions) in the same USER REQUEST / SKILL INSTRUCTIONS shape as
server.skills._tool_run_skill, so emits_prompt=True keeps it out of the
budgeter.
"""


def render_prompt(tool_input: dict, body: str) -> str:
    input_lines = "\n".join(
        f"{k}: {v}" for k, v in tool_input.items() if v and k != "__session_id__"
    )
    return f"USER REQUEST:\n{input_lines}\n\nSKILL INSTRUCTIONS:\n{body}"
