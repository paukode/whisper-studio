"""Additional (non-workspace) tools for Whisper Studio.

Split out of the former single ``server/agent_tools.py`` (900 lines) into
focused submodules, one per tool family:

    schemas       — the ``*_TOOL`` schema dicts + the ``AGENT_TOOLS`` list
    config_tools  — config_get / config_set
    skill_tools   — skill_list
    mcp_tools     — list_mcp_resources / read_mcp_resource
    search_tools  — tool_search (progressive tool activation)
    spawn         — spawn_agent / send_message / list_agents (+ cost rollup)
    teams         — team_create / team_delete (+ the shared ``_teams`` store)

Every name external code reads off ``server.agent_tools`` is re-exported here
so importers (server/tool_router.py, server/chat/*, tests) keep working
unchanged. The dispatch that maps a tool name to one of these executors lives
in server/tool_router.py.
"""

from .config_tools import _mask_secrets, execute_config_get, execute_config_set  # noqa: F401
from .mcp_tools import (  # noqa: F401
    execute_list_mcp_resources,
    execute_read_mcp_resource,
)
from .schemas import (  # noqa: F401
    AGENT_TOOL_NAMES,
    AGENT_TOOLS,
    CONFIG_GET_TOOL,
    CONFIG_SET_TOOL,
    LIST_AGENTS_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    NOTIFY_USER_TOOL,
    READ_MCP_RESOURCE_TOOL,
    SEND_MESSAGE_TOOL,
    SKILL_INVOKE_TOOL,
    SKILL_LIST_TOOL,
    SPAWN_AGENT_TOOL,
    TEAM_CREATE_TOOL,
    TEAM_DELETE_TOOL,
    TOOL_SEARCH_TOOL,
)
from .search_tools import (  # noqa: F401
    _ACTIVATE_BATCH_MAX,
    _SCHEMA_BYTES_MAX,
    execute_tool_search,
)
from .skill_tools import execute_skill_list  # noqa: F401
from .spawn import (  # noqa: F401
    DETACHED_PER_SESSION_CAP,
    _record_agent_cost,
    _start_detached_from_tool,
    execute_list_agents,
    execute_send_message,
    execute_spawn_agent,
)
from .teams import (  # noqa: F401
    _teams,
    _terminal_paragraph,
    execute_team_create,
    execute_team_delete,
)
