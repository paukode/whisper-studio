"""Prompt tools — native built-in tools whose output is a model prompt.

verify_change, create_program, and github_actions have no computation of
their own: each call returns an instruction script (the tool's BODY) that the
model then carries out with other tools. They are wired exactly like every
other native tool — schema dict next to the implementation, registered via
``@register_executor``, always in the pool — the only difference is that the
executor emits instructions (``emits_prompt=True``) instead of computed data,
rendering the same USER REQUEST / SKILL INSTRUCTIONS payload that
server.skills._tool_run_skill produces for markdown prompt skills, so
downstream handling (budget bypass, SSE previews) is unchanged.
"""

# Importing the submodules is what runs @register_executor.
from server.prompt_tools import create_program, github_actions, verify_change

PROMPT_TOOLS = [
    create_program.TOOL,
    github_actions.TOOL,
    verify_change.TOOL,
]

__all__ = ["PROMPT_TOOLS", "create_program", "github_actions", "verify_change"]
