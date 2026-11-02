"""Additional (non-workspace) tools for Whisper Studio, one submodule per
tool family:

    schemas:       the ``*_TOOL`` schema dicts + the ``AGENT_TOOLS`` list
    config_tools:  config_get / config_set
    skill_tools:   skill_list
    mcp_tools:     list_mcp_resources / read_mcp_resource
    search_tools:  tool_search (progressive tool activation)
    spawn:         spawn_agent / send_message / list_agents
    cross_session: list_sessions / send_session_message (between sessions,
                   not within one; see spawn's send_message/list_agents)
    teams:         team_create / team_delete (+ the shared ``_teams`` store)
    promote:       promote_agent_type (saves an ephemeral agent_definition
                   used via spawn_agent as a persistent .whisper/agents/
                   <name>.md custom type; see server/agents/custom_config.py)

The package exposes the executors server/tool_router.py dispatches to and
the ``AGENT_TOOLS`` catalog list server/chat/tool_pool.py reads.
"""

from .config_tools import execute_config_get, execute_config_set  # noqa: F401
from .cross_session import (  # noqa: F401
    execute_list_sessions,
    execute_send_session_message,
)
from .mcp_tools import (  # noqa: F401
    execute_list_mcp_resources,
    execute_read_mcp_resource,
)
from .promote import execute_promote_agent_type  # noqa: F401
from .schemas import AGENT_TOOLS  # noqa: F401
from .search_tools import execute_tool_search  # noqa: F401
from .session_search_tool import execute_session_search  # noqa: F401
from .skill_manage import execute_skill_manage  # noqa: F401
from .skill_tools import execute_skill_list  # noqa: F401
from .spawn import (  # noqa: F401
    execute_list_agents,
    execute_send_message,
    execute_spawn_agent,
)
from .teams import (  # noqa: F401
    execute_team_create,
    execute_team_delete,
)
