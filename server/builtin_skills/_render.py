"""Prompt rendering shared by the built-in prompt tools.

Mirrors server.skills._tool_run_skill exactly: the tool result is a model
PROMPT (the user's arguments followed by the skill instructions), so the
executors registering with emits_prompt=True keep the same payload shape the
markdown prompt skills produced.
"""


def render_prompt(tool_input: dict, body: str) -> str:
    input_lines = "\n".join(
        f"{k}: {v}" for k, v in tool_input.items() if v and k != "__session_id__"
    )
    return f"USER REQUEST:\n{input_lines}\n\nSKILL INSTRUCTIONS:\n{body}"
