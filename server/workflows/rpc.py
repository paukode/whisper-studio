"""ndjson JSON-RPC framing between the Python orchestrator and the Node harness.

One compact JSON object per line over the harness's stdio. The harness sends
requests (``agent``/``workflow``/``budget_spent``, with an ``id``) and
notifications (``phase``/``log``/``meta``/``done``/``fatal``, no ``id``); the
orchestrator replies ``{id, result}`` or ``{id, error}`` and pushes
``start``/``cancel`` control messages. Request/response correlation and the pump
loop live in runtime.py — this module owns only framing and message shapes.
"""

from __future__ import annotations

import json

# A single line is one JSON object; cap it so a runaway harness can't OOM the
# reader (a giant tool_result is sliced upstream long before this).
MAX_LINE_BYTES = 10 * 1024 * 1024

# Error-type strings shared with the harness (it maps them to typed JS errors).
ERR_BUDGET = "BudgetExceededError"
ERR_AGENT_CAP = "AgentCapError"
ERR_CANCELLED = "CancelledError"
ERR_DEPTH = "WorkflowDepthError"
ERR_INTERNAL = "InternalError"


class RpcError(Exception):
    """A framing-level failure (oversized or malformed line)."""


def dumps_line(obj: dict) -> str:
    """Encode one message as a single newline-terminated JSON line."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def loads_line(line: str | bytes) -> dict:
    """Decode one ndjson line into a message dict."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise RpcError(f"rpc line exceeds {MAX_LINE_BYTES} bytes")
        line = line.decode("utf-8", "replace")
    elif len(line) > MAX_LINE_BYTES:
        raise RpcError(f"rpc line exceeds {MAX_LINE_BYTES} bytes")
    line = line.strip()
    if not line:
        raise RpcError("empty rpc line")
    try:
        obj = json.loads(line)
    except (ValueError, TypeError) as e:
        raise RpcError(f"invalid rpc json: {e}") from e
    if not isinstance(obj, dict):
        raise RpcError("rpc message must be a JSON object")
    return obj


def is_request(msg: dict) -> bool:
    """Harness→Python request (expects a response): has an id and a method."""
    return "id" in msg and msg.get("id") is not None and "method" in msg


def is_notification(msg: dict) -> bool:
    """Harness→Python notification (fire-and-forget): a method, no id."""
    return "method" in msg and msg.get("id") is None


# ── message builders (Python → harness) ──────────────────────────────────────


def response(msg_id, result) -> dict:
    return {"id": msg_id, "result": result}


def error_response(msg_id, err_type: str, message: str) -> dict:
    return {"id": msg_id, "error": {"type": err_type, "message": message}}


def control(method: str, params: dict | None = None) -> dict:
    """A start/cancel control message pushed to the harness."""
    out: dict = {"method": method}
    if params is not None:
        out["params"] = params
    return out
