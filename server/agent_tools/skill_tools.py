"""Executor for skill_list (skill_invoke itself is dispatched in server/skills.py)."""

import json


def execute_skill_list() -> str:
    from server.skills import SKILLS

    result = [
        {"name": name, "description": s.get("description", ""), "triggers": s.get("triggers", "")}
        for name, s in SKILLS.items()
    ]
    return json.dumps({"skills": result, "count": len(result)})
