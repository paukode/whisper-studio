"""Built-in prompt tools — product-owned skill bodies that ship as code.

These were user-editable prompt skills under /skills/ (verify_change,
create_program, github_actions). Their markdown bodies ARE the behavior, so
they now live here, versioned and tested with the server, out of reach of the
Skills panel's edit/delete surface. Each submodule registers an
``emits_prompt=True`` executor rendering the same USER REQUEST / SKILL
INSTRUCTIONS payload that server.skills._tool_run_skill produces for markdown
prompt skills, so downstream handling (budget bypass, SSE previews) is
unchanged.
"""

# Importing the submodules is what runs @register_executor.
from server.builtin_skills import create_program, github_actions, verify_change

BUILTIN_SKILL_TOOLS = [
    create_program.TOOL,
    github_actions.TOOL,
    verify_change.TOOL,
]

__all__ = ["BUILTIN_SKILL_TOOLS", "create_program", "github_actions", "verify_change"]
