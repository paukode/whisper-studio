"""The @ mentions of a chat message, resolved once when its turn starts.

The composer's @ menu inserts them (src/hooks/useChatAutocomplete.ts):

- ``@server:uri`` is an MCP resource. Its text goes into the message, ahead
  of what the user typed.
- ``@agent-<type>`` is an agent type. The model is asked to hand the task to
  that agent with spawn_agent (agent_hint). The mention stays in the text.
- ``@<session>`` is another chat session. Its recent conversation goes into
  the message (server/agent_tools/cross_session.py).
- ``@index`` and ``@docs`` are read earlier, by the grounding router
  (server/index/query_gate.py).

Files and folders are not mentions in the text: the composer attaches them.
A mention that names nothing real stays in the text as typed.
"""

import logging
import re

log = logging.getLogger("whisper-studio")

# A mention starts the message or follows whitespace, so "a@b.com" is never one.
_RESOURCE = re.compile(r"(?:^|(?<=\s))@([A-Za-z0-9_.-]+):(\S+)")
_AGENT = re.compile(r"(?:^|(?<=\s))@agent-([A-Za-z0-9_-]+)")
# Sentence punctuation typed right after a mention is not part of the URI.
_TRAILING = ".,;:!?)]}'\""
# The same cap as one attached document (MAX_ATTACHMENT_CHARS).
_RESOURCE_TEXT_MAX = 150_000


async def _resource_blocks(question: str) -> list[str]:
    from server import mcp_catalog

    blocks: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in _RESOURCE.finditer(question):
        server = mcp_catalog.server_by_mention(m.group(1))
        uri = m.group(2).rstrip(_TRAILING)
        if server is None or (server, uri) in seen:
            continue
        seen.add((server, uri))
        try:
            text = await mcp_catalog.read_resource(server, uri)
        except mcp_catalog.CatalogError as e:
            log.info("@%s:%s was not read: %s", m.group(1), uri, e)
            continue
        if len(text) > _RESOURCE_TEXT_MAX:
            text = text[:_RESOURCE_TEXT_MAX] + "\n... (truncated)"
        blocks.append(f"[MCP resource {uri} from {server}]\n{text}")
    return blocks


def _agent_mentions(question: str, workspace_path: str | None) -> list[str]:
    if "@agent-" not in question:
        return []
    from server.agents.config import spawnable_agent_types

    known = {t["name"] for t in spawnable_agent_types(workspace_path)}
    named: list[str] = []
    for m in _AGENT.finditer(question):
        if m.group(1) in known and m.group(1) not in named:
            named.append(m.group(1))
    return named


async def resolve_mentions(
    question: str, session_id: str, workspace_path: str | None
) -> tuple[str, list[str]]:
    """The message with its mentions resolved, and the agent types it names."""
    if "@" not in question:
        return question, []
    from server.agent_tools.cross_session import resolve_at_session_mentions

    agents = _agent_mentions(question, workspace_path)
    blocks = await _resource_blocks(question)
    question = resolve_at_session_mentions(question, session_id)
    return "\n\n".join([*blocks, question]), agents


def agent_hint(agents: list[str]) -> str:
    """The line that gets the named agents started. No provider is sent a
    forced tool_choice (several models reject one), so the model is asked
    plainly, as for a skill the user picked."""
    if len(agents) == 1:
        return (
            f"The user asked for the `{agents[0]}` agent. Call spawn_agent with "
            f"agent_type `{agents[0]}` for this task, and write its task from the "
            f"user's message."
        )
    names = ", ".join(f"`{a}`" for a in agents)
    return (
        f"The user asked for these agents: {names}. Call spawn_agent once for "
        f"each, with that agent_type, and write each task from the user's message."
    )
