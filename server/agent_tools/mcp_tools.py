"""Executors for list_mcp_resources / read_mcp_resource."""

import json


def execute_list_mcp_resources(tool_input: dict) -> str:
    from server.mcp_catalog import all_resources

    server_filter = tool_input.get("server")
    resources = all_resources()
    if server_filter:
        resources = [r for r in resources if r.get("server") == server_filter]
    return json.dumps({"resources": resources, "count": len(resources)})


async def execute_read_mcp_resource(tool_input: dict) -> str:
    from server.mcp_catalog import read_resource

    server = tool_input.get("server", "")
    uri = tool_input.get("uri", "")
    try:
        content = await read_resource(server, uri)
        return json.dumps({"server": server, "uri": uri, "content": content})
    except Exception as e:
        return json.dumps({"error": str(e)})
