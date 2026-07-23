"""Executor + schema for ``read_cached_result``.

When the chat budgeter truncates an oversize tool result, the full output is
persisted to the result cache (server/infrastructure/result_cache.py) and the
truncation note tells the model to call this tool. Always in the pool — it
must work with no workspace connected, and the cache lives outside any
workspace root by design.
"""

from server.executors import register_executor
from server.infrastructure import result_cache

RESULT_CACHE_TOOLS = [
    {
        "name": "read_cached_result",
        "description": (
            "Read the full output of an earlier tool call that was truncated for "
            "length. The truncation note names the cache filename. Returns "
            "numbered lines; use offset/limit to page through large results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Cache filename from the truncation note (e.g. 'aws_boto3_1784016324526.txt')",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start line (1-based). Defaults to 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. Defaults to the whole file (capped ~48KB).",
                },
            },
            "required": ["filename"],
        },
    },
]


@register_executor("read_cached_result", read_only=True, concurrent_safe=True)
def _exec_read_cached_result(tool_input, transcript, current_attachments):
    tool_input.pop("__session_id__", None)
    filename = str(tool_input.get("filename", "") or "")
    offset = tool_input.get("offset", 1)
    limit = tool_input.get("limit")
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    return result_cache.read(filename, offset=offset, limit=limit)
