"""What the connected MCP servers offer besides tools: prompts and resources.

The composer lists both from GET /api/mcp/servers. A prompt runs as
``/server:prompt args``: render_prompt turns it into text, which the chat
sends as an attachment of the message, so later turns still see it. A
resource is mentioned as ``@server:uri``: server/chat/mentions.py reads it
into the message. The model reaches the same resources through the
list_mcp_resources and read_mcp_resource tools (server/agent_tools/mcp_tools.py).
"""

import asyncio
import re
import shlex

from server.mcp import _call_timeout_s, mcp_manager

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class CatalogError(ValueError):
    """A prompt or resource that cannot be used, with the reason for the user."""


def mention_name(name: str) -> str:
    """A server or prompt name as /server:prompt and @server:uri spell it:
    each character outside letters, digits, '_', '.' and '-' becomes '_'."""
    return _UNSAFE.sub("_", name)


def _connected(server: str) -> dict:
    info = mcp_manager.connected_sessions().get(server)
    if info is None:
        raise CatalogError(f"The MCP server {server} is not connected.")
    return info


def server_by_mention(name: str) -> str | None:
    """The connected server that ``name`` (its mention spelling) refers to."""
    for server in mcp_manager.connected_sessions():
        if mention_name(server) == name:
            return server
    return None


def server_prompts(server: str) -> list[dict]:
    """A connected server's prompts, as the composer lists them."""
    info = mcp_manager.connected_sessions().get(server) or {}
    return [
        {
            "name": prompt.name,
            "command": f"{mention_name(server)}:{mention_name(prompt.name)}",
            "description": prompt.description or prompt.title or "",
            "arguments": [
                {
                    "name": arg.name,
                    "description": arg.description or "",
                    "required": bool(arg.required),
                }
                for arg in prompt.arguments or []
            ],
        }
        for prompt in info.get("prompts", {}).values()
    ]


def server_resources(server: str) -> list[dict]:
    """A connected server's resources, as the composer lists them."""
    info = mcp_manager.connected_sessions().get(server) or {}
    return [
        {
            "uri": uri,
            "mention": f"{mention_name(server)}:{uri}",
            "name": getattr(resource, "name", "") or uri,
            "description": getattr(resource, "description", "") or "",
        }
        for uri, resource in info.get("resources", {}).items()
    ]


def all_resources() -> list[dict]:
    """Every resource of every connected server, each with its server."""
    return [
        {**resource, "server": server}
        for server in mcp_manager.connected_sessions()
        for resource in server_resources(server)
    ]


def _contents_text(contents) -> str:
    parts = []
    for content in contents:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif getattr(content, "blob", None) is not None:
            mime = getattr(content, "mimeType", None) or "binary"
            parts.append(f"[{mime} content, {len(content.blob)} bytes, not shown]")
        else:
            parts.append(str(content))
    return "\n".join(parts)


async def read_resource(server: str, uri: str) -> str:
    """The text of one resource. Raises CatalogError with the reason."""
    info = _connected(server)
    try:
        result = await asyncio.wait_for(
            info["session"].read_resource(uri), _call_timeout_s(info.get("config") or {})
        )
    except Exception as e:  # noqa: BLE001 - the server's reason goes to the user
        raise CatalogError(f"{server} could not read {uri}: {e or type(e).__name__}") from None
    return _contents_text(result.contents)


def _arguments(command: str, declared: list, args_text: str) -> dict[str, str]:
    """Map the words after /server:prompt onto the prompt's arguments, in
    order. The last argument takes the rest of the line. Quotes group words."""
    try:
        words = shlex.split(args_text)
    except ValueError:  # an open quote: take the words as they are
        words = args_text.split()
    values: dict[str, str] = {}
    for i, arg in enumerate(declared):
        if i >= len(words):
            break
        last = i == len(declared) - 1
        values[arg.name] = " ".join(words[i:]) if last else words[i]
    missing = [arg.name for arg in declared if arg.required and arg.name not in values]
    if missing:
        raise CatalogError(f"/{command} needs: {', '.join(missing)}.")
    return values


def _message_text(content) -> str:
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    resource = getattr(content, "resource", None)
    if resource is not None:
        body = getattr(resource, "text", None)
        if isinstance(body, str):
            return f"[Resource {resource.uri}]\n{body}"
        return f"[Resource {resource.uri}: binary content, not shown]"
    uri = getattr(content, "uri", None)
    if uri is not None:
        return f"[Resource link {uri}]"
    return f"[{getattr(content, 'type', 'other')} content, not shown]"


async def render_prompt(command: str, args_text: str) -> tuple[str, str]:
    """Run ``/command args`` on its server and return (filename, text) for
    the attachment that carries the prompt. Raises CatalogError."""
    server_name, _, prompt_name = command.partition(":")
    server = server_by_mention(server_name)
    if server is None:
        raise CatalogError(f"No connected MCP server is named {server_name}.")
    info = _connected(server)
    prompt = next(
        (p for p in info.get("prompts", {}).values() if mention_name(p.name) == prompt_name),
        None,
    )
    if prompt is None:
        raise CatalogError(f"{server} has no prompt named {prompt_name}.")
    arguments = _arguments(command, prompt.arguments or [], args_text)
    try:
        result = await asyncio.wait_for(
            info["session"].get_prompt(prompt.name, arguments),
            _call_timeout_s(info.get("config") or {}),
        )
    except Exception as e:  # noqa: BLE001 - the server's reason goes to the user
        raise CatalogError(
            f"{server} could not run {prompt.name}: {e or type(e).__name__}"
        ) from None
    messages = getattr(result, "messages", None)
    if messages is None:
        raise CatalogError(f"{prompt.name} asks for more input, which the app cannot give it.")
    if len(messages) == 1 and messages[0].role == "user":
        body = _message_text(messages[0].content)
    else:
        body = "\n\n".join(f"{m.role.capitalize()}: {_message_text(m.content)}" for m in messages)
    return f"/{command} (MCP prompt)", f"[MCP prompt /{command} from {server}]\n\n{body}"
