"""What an agent run may execute, and what each of its rounds is offered.

Two layers, the split Claude Code uses for subagents: the tool list decides
what the model is shown, and a check at execution decides what runs. Agents
used to have only the first. Their array was built once from the chat core
set plus the session's activations and then filtered by the agent's config,
and nothing checked a name before it ran. A model emits names it read
elsewhere (the deferred-tool index, a tool_search result, its brief, a file
it opened), and agents approve their own [WS_APPROVAL] gates, so a read-only
agent could write. Field case: a session summary run offered four tools ran
tool_search, memory_list and memory_write and saved a long-term memory file.

ENTITLED, fixed for the run: the agent's catalog (the chat catalog outside
ultracode, plus the agent runtime tools, delegation stripped at the depth
limit) through its own filters (allowed_tools, read_only). The tool executor
refuses every other name, and tool_search finds nothing outside it. A call to
an entitled tool that is not offered yet still runs, as in chat: the boundary
is the entitlement, not the loading step.

OFFERED, rebuilt every round from the entitlement: a whitelisted agent gets
its whole list. A whitelist is short and complete by construction, and
deferral left the memory agents without the memory tools they exist to use
(tool_search is not on their list). Any other agent gets the core set, the
parent session's activations and its own, and the deferred index lists the
rest. Its tool_search loads land in its own activation set, so they are
offered from its next round (GPT agents can only call tools they were
offered) and the parent chat's tools array is left alone.
"""

from dataclasses import dataclass

from server.agents.config import AgentConfig, filter_tools_for_agent


def activation_key(agent_id: str) -> str:
    """The key an agent run's tool_search activations live under, apart from
    the session it shares with its parent."""
    return f"agent:{agent_id}"


@dataclass(frozen=True)
class ToolScope:
    """What one agent run may execute. The tool executor refuses any name
    outside ``permitted``; tool_router bounds tool_search to it and activates
    under ``activation_key``."""

    permitted: frozenset[str]
    activation_key: str


def _first_of_each_name(tools: list[dict]) -> list[dict]:
    # MCP servers and plugins may contribute duplicate names, and Bedrock
    # rejects a request whose tool names are not unique.
    seen: set[str] = set()
    out: list[dict] = []
    for tool in tools:
        if tool["name"] not in seen:
            seen.add(tool["name"])
            out.append(tool)
    return out


class AgentToolAccess:
    """The entitlement of one agent run and the tools each round offers."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_id: str,
        depth: int,
        session_id: str,
        ws_connected: bool,
    ):
        from server.agents.tools import (
            get_agent_runtime_tools,
            strip_delegation_tools_at_depth_limit,
        )
        from server.chat.tool_partition import core_names
        from server.chat.tool_pool import assemble_full_catalog
        from server.infrastructure.feature_flags import is_enabled

        catalog = assemble_full_catalog(plan_mode=False, ws_connected=ws_connected)
        in_catalog = {t["name"] for t in catalog}
        core = core_names()
        # A name in both lists keeps the chat's schema when it is core
        # (spawn_agent, with isolation and detach) and the runtime's otherwise
        # (send_message, list_agents), as the agent pool always has.
        runtime = [
            t
            for t in get_agent_runtime_tools(agent_id, depth)
            if not (t["name"] in core and t["name"] in in_catalog)
        ]
        runtime_names = {t["name"] for t in runtime}
        catalog = [t for t in catalog if t["name"] not in runtime_names]

        def entitled(tools: list[dict]) -> list[dict]:
            stripped = strip_delegation_tools_at_depth_limit(tools, depth)
            return _first_of_each_name(filter_tools_for_agent(stripped, config))

        self._catalog = entitled(catalog)
        # The agent runtime tools (messaging, coordination) are offered every
        # round, never deferred.
        self._runtime = entitled(runtime)
        self._session_id = session_id
        self._progressive = (
            config.allowed_tools is None and bool(session_id) and is_enabled("progressive_tools")
        )
        self.scope = ToolScope(
            permitted=frozenset(t["name"] for t in [*self._catalog, *self._runtime]),
            activation_key=activation_key(agent_id),
        )

    def offered(self) -> list[dict]:
        """This round's tools array: every entitled tool when nothing is
        deferred, else the core set plus the session's and the agent's own
        activations, then the runtime tools."""
        if not self._progressive:
            return [*self._catalog, *self._runtime]
        from server.chat.tool_activation import get_ordered
        from server.chat.tool_partition import partition_pool

        activated = [*get_ordered(self._session_id), *get_ordered(self.scope.activation_key)]
        advertised, _deferred, _core_count = partition_pool(self._catalog, activated)
        return [*advertised, *self._runtime]

    def deferred_index(self) -> str:
        """The system-prompt index of entitled tools not offered yet, or ""
        when there are none or the agent is not offered tool_search, the only
        way to load them."""
        from server.chat.tool_index import build_deferred_index

        offered = {t["name"] for t in self.offered()}
        if "tool_search" not in offered:
            return ""
        return build_deferred_index([t for t in self._catalog if t["name"] not in offered])
