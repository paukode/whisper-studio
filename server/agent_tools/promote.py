"""promote_agent_type executor.

Writes a session's ephemeral agent definition (server/agents/custom_config.py,
registered by a prior spawn_agent call's ``agent_definition``) out as a
persistent ``.whisper/agents/<name>.md`` custom type — the reverse of the Part
1 loader. The definition is RE-VALIDATED (not blindly trusted) through
build_agent_config at promote time; see custom_config.promote_ephemeral_type.
"""

import json


def execute_promote_agent_type(tool_input: dict, session_id: str) -> str:
    from server.agents.custom_config import promote_ephemeral_type
    from server.workspace import get_workspace_path

    name = str(tool_input.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"})
    scope = tool_input.get("scope") or "project"
    if scope not in ("project", "user"):
        return json.dumps({"error": "scope must be 'project' or 'user'"})

    try:
        result = promote_ephemeral_type(
            session_id, name, workspace_path=get_workspace_path(), scope=scope
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if result is None:
        return json.dumps(
            {
                "error": (
                    f"No ephemeral agent type named {name!r} was used yet this "
                    "session. Use spawn_agent with agent_definition first."
                )
            }
        )
    config, path = result
    return json.dumps(
        {
            "saved": True,
            "name": config.agent_type,
            "path": path,
            "scope": scope,
            "read_only": config.read_only,
            "tools": sorted(config.allowed_tools) if config.allowed_tools else None,
        }
    )
