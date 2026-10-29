"""Executor for skill_list (skill_invoke itself is dispatched in server/skills.py)."""

import json

from server.executors import register_executor


def execute_skill_list() -> str:
    from server.skills import SKILLS

    result = [
        {"name": name, "description": s.get("description", ""), "triggers": s.get("triggers", "")}
        for name, s in SKILLS.items()
    ]
    return json.dumps({"skills": result, "count": len(result)})


# tool_router.py dispatches skill_list on its own branch. The registry entry
# marks it as a read, so a tool script (server/workflows/runtime.py) can call it.
@register_executor("skill_list", read_only=True, concurrent_safe=True)
def _exec_skill_list(tool_input, transcript, current_attachments):
    return execute_skill_list()
